const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const CLIENT_ID_STORAGE_KEY = 'gnani_client_id';

function generateClientId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export function getClientId(): string {
  try {
    let clientId = localStorage.getItem(CLIENT_ID_STORAGE_KEY);
    if (!clientId) {
      clientId = generateClientId();
      localStorage.setItem(CLIENT_ID_STORAGE_KEY, clientId);
    }
    return clientId;
  } catch {
    return generateClientId();
  }
}

export class ApiError extends Error {
  error_code: string;

  constructor(error_code: string, message: string) {
    super(message);
    self.name = 'ApiError';
    this.error_code = error_code;
  }
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let errorCode = 'HTTP_ERROR';
    let errorMessage = `HTTP Error ${response.status}: ${response.statusText}`;

    try {
      const errorJson = await response.json();
      if (errorJson.error_code) {
        errorCode = errorJson.error_code;
      }
      if (errorJson.message) {
        errorMessage = errorJson.message;
      } else if (errorJson.detail) {
        errorMessage = typeof errorJson.detail === 'string' 
          ? errorJson.detail 
          : JSON.stringify(errorJson.detail);
      }
    } catch {
      // Body wasn't JSON
    }

    throw new ApiError(errorCode, errorMessage);
  }

  return response.json() as Promise<T>;
}

export async function apiGet<T>(path: string): Promise<T> {
  const url = `${BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
  const response = await fetch(url, {
    method: 'GET',
    headers: {
      'Accept': 'application/json',
      'X-Client-ID': getClientId(),
    },
  });
  return handleResponse<T>(response);
}

export async function apiPostMultipart<T>(path: string, formData: FormData): Promise<T> {
  const url = `${BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'X-Client-ID': getClientId(),
    },
    body: formData,
  });
  return handleResponse<T>(response);
}

export async function apiPost<T>(path: string, body?: any): Promise<T> {
  const url = `${BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      'X-Client-ID': getClientId(),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  return handleResponse<T>(response);
}

