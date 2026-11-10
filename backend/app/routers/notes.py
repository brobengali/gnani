import os
import shutil
import tempfile
from uuid import UUID
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy.orm import Session

from app.core.limiter import limiter
from app.db.session import get_db
from app.models.audio_note import AudioNote, NoteStatus
from app.schemas.audio_note import AudioNoteCreateResponse, AudioNoteOut, AudioNoteDetailOut
from app.services.audio_validation_service import AudioValidationService, AudioValidationError
from app.services.storage_service import StorageService, LOCAL_STORAGE_DIR
from app.queues.queue_service import QueueService, InvalidStateError, DuplicateJobError

router = APIRouter(prefix="/notes", tags=["notes"])
storage_service = StorageService()


def get_client_id(x_client_id: str | None = Header(default=None, alias="X-Client-ID")) -> str:
    if not x_client_id or not x_client_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Client-ID header.",
        )
    return x_client_id.strip()


def _get_owned_note(note_id: UUID, client_id: str, db: Session) -> AudioNote:
    note = (
        db.query(AudioNote)
        .filter(AudioNote.id == note_id, AudioNote.client_id == client_id)
        .first()
    )
    if not note:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AudioNote not found")
    return note


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=AudioNoteCreateResponse)
@limiter.limit("10/minute")
async def upload_audio_note(
    request: Request,
    audio: UploadFile = File(...),
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db)
):
    filename = audio.filename or "audio_file"
    content_type = audio.content_type or ""

    suffix = os.path.splitext(filename)[1]
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp_file.name

    try:
        with temp_file as buffer:
            shutil.copyfileobj(audio.file, buffer)

        file_size = os.path.getsize(temp_path)

        # 1. Validate extension and MIME type
        AudioValidationService.validate_extension_and_mime(filename, content_type)

        # 2. Validate file size
        AudioValidationService.validate_file_size(file_size, max_mb=100)

        # 3. Check for file corruption
        if AudioValidationService.is_corrupt(temp_path):
            raise AudioValidationError(
                code="CORRUPT_FILE",
                message="File cannot be opened or parsed as valid audio."
            )

        # 4. Extract and validate audio duration
        duration = AudioValidationService.get_duration_seconds(temp_path)
        AudioValidationService.validate_duration(duration, min_seconds=1)

        # Upload to S3-compatible storage
        storage_key = storage_service.generate_unique_storage_key(filename)
        storage_service.upload_file(temp_path, storage_key, content_type)

        # Persist AudioNote record in DB with status QUEUED
        note = AudioNote(
            client_id=client_id,
            filename=filename,
            storage_key=storage_key,
            mime_type=content_type,
            file_size=file_size,
            duration_seconds=int(round(duration)),
            status=NoteStatus.QUEUED,
        )
        db.add(note)
        db.commit()
        db.refresh(note)

        # Enqueue background transcription job
        try:
            QueueService.enqueue_transcription(note.id, db=db)
        except Exception as q_err:
            print(f"Warning: Failed to enqueue transcription for note {note.id}: {q_err}")

        return AudioNoteCreateResponse(id=note.id, status=note.status.value)

    except AudioValidationError as err:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error_code": err.code, "message": err.message}
        )
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


@router.get("", response_model=list[AudioNoteOut])
def list_audio_notes(
    x_client_id: str | None = Header(default=None, alias="X-Client-ID"),
    db: Session = Depends(get_db),
):
    if not x_client_id or not x_client_id.strip():
        return []
    notes = (
        db.query(AudioNote)
        .filter(AudioNote.client_id == x_client_id.strip())
        .order_by(AudioNote.created_at.desc())
        .all()
    )
    return notes


@router.get("/stream-file/{storage_key:path}")
def stream_local_file(storage_key: str):
    file_path = LOCAL_STORAGE_DIR / storage_key
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found on storage disk.")
    return FileResponse(path=file_path, media_type="audio/mpeg")


@router.get("/{note_id}", response_model=AudioNoteDetailOut)
def get_audio_note_detail(
    note_id: UUID,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
):
    return _get_owned_note(note_id, client_id, db)


@router.get("/{note_id}/audio")
def get_audio_note_playback_url(
    note_id: UUID,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(note_id, client_id, db)
    url = storage_service.generate_presigned_url(note.storage_key, expires_in_seconds=900)
    return {"url": url}


@router.post("/{note_id}/retry", status_code=status.HTTP_202_ACCEPTED, response_model=AudioNoteCreateResponse)
@limiter.limit("10/minute")
def retry_transcription_route(
    request: Request,
    note_id: UUID,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(note_id, client_id, db)

    try:
        QueueService.retry_transcription(note_id, db=db)
        db.refresh(note)
        return AudioNoteCreateResponse(id=note.id, status=note.status.value)
    except InvalidStateError as err:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error_code": "INVALID_STATE", "message": str(err)}
        )
    except DuplicateJobError as err:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error_code": "DUPLICATE_JOB", "message": str(err)}
        )


@router.post("/{note_id}/retry-summary", status_code=status.HTTP_202_ACCEPTED, response_model=AudioNoteCreateResponse)
@limiter.limit("10/minute")
def retry_summary_route(
    request: Request,
    note_id: UUID,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(note_id, client_id, db)

    try:
        QueueService.retry_summary(note_id, db=db)
        db.refresh(note)
        return AudioNoteCreateResponse(id=note.id, status=note.status.value)
    except InvalidStateError as err:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error_code": "INVALID_STATE", "message": str(err)}
        )
    except DuplicateJobError as err:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error_code": "DUPLICATE_JOB", "message": str(err)}
        )
