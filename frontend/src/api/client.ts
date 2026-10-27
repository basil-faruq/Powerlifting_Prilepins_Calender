import createClient from "openapi-fetch";
import type { paths } from "./schema";

// `localhost`, not `127.0.0.1`: the backend's CORS list names http://localhost:5173.
export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export const api = createClient<paths>({ baseUrl: API_URL });
