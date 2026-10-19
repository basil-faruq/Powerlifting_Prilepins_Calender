import { EmptyState } from "@/components/states/EmptyState";

/** A route whose screen belongs to a later spec. Real pages replace these one by one. */
export function Placeholder({ title, spec }: { title: string; spec: number }) {
  return <EmptyState title={title} description={`This screen is built in spec ${spec}.`} />;
}
