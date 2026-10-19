import { useEffect, useState } from "react";

/**
 * `value`, but only after it has stopped changing for `delayMs`. Pass something with
 * stable identity (a string, a number): an object rebuilt every render would restart
 * the timer every render and never settle.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}
