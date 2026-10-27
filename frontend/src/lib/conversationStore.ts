/**
 * Which conversation an athlete is in the middle of.
 *
 * Kept per athlete in `localStorage` so a reload continues the same thread rather
 * than silently starting a new one. Every access is guarded: a private window, or
 * blocked site data, throws on read as readily as on write, and a chat panel that
 * cannot remember its thread must still work.
 *
 * It lives in `lib/` because both the panel and the send mutation need it. The
 * mutation is what persists a NEW id, because TanStack Query drops a caller's own
 * callbacks when that component unmounted first -- closing the calendar's drawer
 * while the model is thinking would otherwise orphan the thread it just started.
 */
const storageKey = (athleteId: string) => `chat.conversation.${athleteId}`;

export function loadConversationId(athleteId: string): string | null {
  try {
    return window.localStorage.getItem(storageKey(athleteId));
  } catch {
    return null;
  }
}

export function saveConversationId(athleteId: string, conversationId: string): void {
  try {
    window.localStorage.setItem(storageKey(athleteId), conversationId);
  } catch {
    // Not being able to remember the thread is not worth interrupting anyone over.
  }
}

export function clearConversationId(athleteId: string): void {
  try {
    window.localStorage.removeItem(storageKey(athleteId));
  } catch {
    // As above.
  }
}
