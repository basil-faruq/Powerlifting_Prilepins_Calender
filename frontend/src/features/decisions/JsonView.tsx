import { Box, Button, Code, Collapsible, HStack, Stack, Text } from "@chakra-ui/react";

/**
 * A collapsed-by-default view of whatever the recorder stored.
 *
 * No library (spec 21 §9). Four rules, each deliberate:
 *
 * - **Depth, not size, decides what is open.** `openDepth` levels start open and
 *   everything under them starts shut, so the same summary always looks the same. A
 *   size threshold would make one `output_summary` expand and its neighbour not.
 * - **Key order is the server's.** `proposal_id, block_id, session_count` is how the
 *   summary was built and how it reads; alphabetising would scramble a record whose
 *   whole purpose is to be read as written.
 * - **Nothing is reformatted.** A number prints as `String(n)`, so `0.0423` stays
 *   `0.0423` -- rounding belongs to the stats section, not to the raw record. A string
 *   prints inside quotes, which is how a reader SEES that `"92.50"` is the exact
 *   decimal string the backend sent and not a float (CLAUDE.md #4).
 * - **Closed content is unmounted**, so a nine-call decision does not build nine JSON
 *   trees nobody opened, and a screen reader is never offered hidden content.
 */

/**
 * The backend already truncates a recorded input at 300 characters; this is the width
 * a line can take before the rest moves into the tooltip.
 */
const STRING_CHARS = 120;

/** JSON cannot cycle, but a pathological nesting still should not recurse forever. */
const MAX_DEPTH = 6;

interface Props {
  value: unknown;
  /** Names the group for a screen reader: "plan_block input". */
  label: string;
  /** 1 = this value's own keys visible, everything inside them shut. 0 = all shut. */
  openDepth?: number;
}

export function JsonView({ value, label, openDepth = 1 }: Props) {
  const entries = childEntries(value);

  return (
    <Box role="group" aria-label={label} fontSize="xs" minW="0">
      {entries === null ? (
        <Leaf value={value} />
      ) : entries.length === 0 ? (
        <Text color="fg.muted">nothing recorded</Text>
      ) : (
        <Stack gap="1" minW="0">
          {entries.map(([key, child]) => (
            <JsonNode key={key} name={key} value={child} depth={1} openDepth={openDepth} />
          ))}
        </Stack>
      )}
    </Box>
  );
}

interface NodeProps {
  name: string;
  value: unknown;
  depth: number;
  openDepth: number;
}

function JsonNode({ name, value, depth, openDepth }: NodeProps) {
  const entries = childEntries(value);

  if (entries === null) {
    return (
      <HStack gap="2" align="start" minW="0">
        <Key name={name} />
        <Leaf value={value} />
      </HStack>
    );
  }

  if (depth >= MAX_DEPTH) {
    return (
      <HStack gap="2" minW="0">
        <Key name={name} />
        <Text color="fg.muted" title="Nested too deeply to show here">
          …
        </Text>
      </HStack>
    );
  }

  // Nothing to expand: an empty container says so on its own line rather than
  // offering a button that does nothing.
  if (entries.length === 0) {
    return (
      <HStack gap="2" minW="0">
        <Key name={name} />
        <Code size="sm" variant="plain" color="fg.muted">
          {Array.isArray(value) ? "[]" : "{}"}
        </Code>
      </HStack>
    );
  }

  return (
    <Collapsible.Root defaultOpen={depth < openDepth} lazyMount unmountOnExit minW="0">
      <Collapsible.Trigger asChild>
        <Button
          variant="ghost"
          size="xs"
          px="1"
          h="auto"
          py="0.5"
          fontWeight="normal"
          justifyContent="flex-start"
        >
          <Text as="span" fontFamily="mono" fontWeight="medium">
            {name}
          </Text>
          <Text as="span" color="fg.muted">
            {Array.isArray(value)
              ? `[${entries.length}]`
              : `{${entries.length} ${entries.length === 1 ? "key" : "keys"}}`}
          </Text>
        </Button>
      </Collapsible.Trigger>
      <Collapsible.Content>
        <Stack gap="1" ps="3" ms="2" borderStartWidth="1px" borderColor="border" minW="0">
          {entries.map(([key, child]) => (
            <JsonNode key={key} name={key} value={child} depth={depth + 1} openDepth={openDepth} />
          ))}
        </Stack>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function Key({ name }: { name: string }) {
  return (
    <Text as="span" fontFamily="mono" fontWeight="medium" flexShrink="0">
      {name}
    </Text>
  );
}

/**
 * A value with no children.
 *
 * Narrowed type by type rather than handed to `String(unknown)`: that is what keeps
 * the type-aware lint rule quiet and, more to the point, means a shape this view
 * cannot print says so instead of rendering "[object Object]".
 */
function Leaf({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return (
      <Code size="sm" variant="plain" color="fg.muted">
        null
      </Code>
    );
  }
  if (typeof value === "string") {
    const clipped = value.length > STRING_CHARS;
    return (
      <Code
        size="sm"
        variant="plain"
        title={clipped ? value : undefined}
        whiteSpace="pre-wrap"
        wordBreak="break-word"
        minW="0"
      >
        {`"${clipped ? `${value.slice(0, STRING_CHARS)}…` : value}"`}
      </Code>
    );
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return (
      <Code size="sm" variant="plain">
        {String(value)}
      </Code>
    );
  }
  return (
    <Text as="span" color="fg.muted" title="Not a value this view can print">
      …
    </Text>
  );
}

/**
 * A value's children as display-name/value pairs, or null if it has none.
 *
 * An array's names are its indices in brackets, so `[0]` is never mistaken for a key
 * called "0". Object keys come back in the order `Object.entries` gives them, which
 * for JSON parsed from the wire is the order the server wrote them.
 */
function childEntries(value: unknown): [string, unknown][] | null {
  if (Array.isArray(value)) return value.map((item, i) => [`[${i}]`, item]);
  if (typeof value === "object" && value !== null) return Object.entries(value);
  return null;
}
