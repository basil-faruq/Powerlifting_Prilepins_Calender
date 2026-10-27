import type { Violation } from "./types";

/**
 * The body of a 4xx this API makes (`ProblemOut` in backend/api/schemas.py). It is
 * not attached to any route via `responses=`, so it is absent from the generated
 * schema and has to be described here -- the one hand-written wire shape.
 */
export interface Problem {
  detail: string;
  violations: Violation[];
  warnings: Violation[];
  problems: string[];
}

interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function violations(value: unknown): Violation[] {
  return Array.isArray(value) ? (value as Violation[]) : [];
}

/** Both 4xx shapes in one: `ProblemOut`, and FastAPI's own `{detail: [{loc, msg}]}` 422. */
export function parseProblem(body: unknown, status: number): Problem {
  const empty = { violations: [], warnings: [], problems: [] };
  if (!isRecord(body)) return { detail: `HTTP ${status}`, ...empty };

  const { detail } = body;
  if (typeof detail === "string") {
    return {
      detail,
      violations: violations(body.violations),
      warnings: violations(body.warnings),
      problems: strings(body.problems),
    };
  }
  if (Array.isArray(detail)) {
    const issues = (detail as ValidationIssue[]).map((issue) => {
      const where = (issue.loc ?? []).filter((part) => part !== "body").join(".");
      return where ? `${where}: ${issue.msg ?? "invalid"}` : (issue.msg ?? "invalid");
    });
    return { detail: issues.join("; "), ...empty, problems: issues };
  }
  return { detail: `HTTP ${status}`, ...empty };
}

/** A response the server gave and we refuse to treat as success. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem;

  constructor(status: number, problem: Problem) {
    super(problem.detail);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }
}

/** No response at all: `fetch` rejects with a TypeError when the server cannot be reached. */
export function isNetworkError(error: unknown): boolean {
  return error instanceof TypeError;
}

/** The sentence to show a person, whatever went wrong. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail;
  if (isNetworkError(error)) return "The backend could not be reached.";
  return error instanceof Error ? error.message : "Something went wrong.";
}
