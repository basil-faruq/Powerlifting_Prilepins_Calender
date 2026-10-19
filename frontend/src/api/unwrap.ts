import { ApiError, parseProblem } from "./errors";

type Result = { data?: unknown; error?: unknown; response: Response };

/**
 * `openapi-fetch` never throws on a 4xx/5xx -- it hands back `{ error }`. TanStack
 * Query needs a throw to see a failure, and a thrown `ApiError` carries the
 * server's own message and violations through to a toast or an inline list.
 *
 * Typed over the whole result rather than over `T` directly: the result is a union
 * of a success member and an error member whose `data` is `undefined`, and asking
 * TypeScript to infer `T` from that union yields `T | undefined`. Excluding
 * `undefined` from the union's `data` gives back exactly the success body.
 */
export async function unwrap<R extends Result>(call: Promise<R>): Promise<Exclude<R["data"], undefined>> {
  const result = await call;
  // Narrow on `data`, not `error`: `error?: undefined` on the success member means
  // checking `error` cannot tell the two members of the union apart.
  if (result.data !== undefined) return result.data as Exclude<R["data"], undefined>;
  // A 2xx with no body (a 204) is a success that carries nothing.
  if (result.response.ok) return undefined as Exclude<R["data"], undefined>;
  throw new ApiError(result.response.status, parseProblem(result.error, result.response.status));
}
