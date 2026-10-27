/** The errors the API client throws (A34.8).
 *
 *  A module of their own, with no fetching in it, so `viewModel/dataState`
 *  can classify a failure without depending on the client that produced it
 *  (A34.5: a view-model imports neither React nor the API client).
 */

/** A34.8: a failed request that remembers what the server said.
 *
 *  It is still an `Error`, so every existing `err instanceof Error ?
 *  err.message : ...` handler is unchanged. What it adds is the HTTP
 *  status, which is the only thing that tells "you may not see this" (403)
 *  from "it is not there, or not visible in your scope" (404) from "the
 *  server did not answer" (0 or 5xx). `viewModel/dataState` reads it; a
 *  page never needs to. */
export class ApiError extends Error {
  /** The HTTP status, or 0 when no response arrived at all. */
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** 401: the session is over. Handled once, globally, by `useAuth`. */
export class AuthError extends ApiError {
  constructor(message = "unauthorized") {
    super(message, 401);
    this.name = "AuthError";
  }
}
