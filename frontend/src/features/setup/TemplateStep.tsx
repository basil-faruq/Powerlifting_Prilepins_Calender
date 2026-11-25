import { Box, Button, Heading, Input, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { ApiError } from "@/api/errors";
import { useImportTemplate } from "@/api/hooks/templates";
import type { TemplateSummaryOut } from "@/api/types";
import { ProblemAlert } from "@/components/ProblemAlert";
import { StepNav } from "./StepNav";

interface Props {
  /** The newest template: the built-in one, or a spreadsheet imported over it. */
  template: TemplateSummaryOut | undefined;
  onNext: () => void;
}

/** "77 entries · 11 weeks · 3 days": what the template is, in the words the spec uses. */
export function templateSummary(t: Pick<TemplateSummaryOut, "entries" | "weeks" | "slots">): string {
  return `${t.entries} entries · ${t.weeks} weeks · ${t.slots} days`;
}

export function TemplateStep({ template, onNext }: Props) {
  const [replacing, setReplacing] = useState(false);
  const importTemplate = useImportTemplate();
  const showPicker = template === undefined || replacing;

  const upload = (file: File | undefined) => {
    if (file === undefined) return;
    // Replacing is the only way past a 409 for a name that already exists.
    importTemplate.mutate({ file, replace: template !== undefined }, { onSuccess: () => setReplacing(false) });
  };

  const error = importTemplate.error;
  const lines =
    error instanceof ApiError
      ? error.problem.problems.length > 0
        ? error.problem.problems
        : [error.problem.detail]
      : error
        ? [error.message]
        : [];

  return (
    <Stack gap="4">
      <Heading size="lg">Your template</Heading>
      <Text color="fg.muted">
        The program: which lift to train, for how many sets and reps, at what share of your 1RM. Every
        weight is computed from the maxes you enter next. To run a different program, use your own
        spreadsheet; only its pattern is read, never the weights it computed or its example maxes.
      </Text>

      {template !== undefined && (
        <Box borderWidth="1px" rounded="md" p="4">
          <Text fontWeight="semibold">{template.name}</Text>
          <Text>{templateSummary(template)}</Text>
          <Text fontSize="sm" color="fg.muted">
            {template.source_file ? `from ${template.source_file}` : "built in"}
          </Text>
          {!replacing && (
            <Button mt="3" size="sm" variant="outline" onClick={() => setReplacing(true)}>
              Use your own spreadsheet
            </Button>
          )}
        </Box>
      )}

      {showPicker && (
        <Stack gap="2">
          <Text fontWeight="medium">{template === undefined ? "Choose your .xlsx" : "Choose the replacement .xlsx"}</Text>
          <Input
            type="file"
            accept=".xlsx"
            aria-label="Template spreadsheet"
            disabled={importTemplate.isPending}
            onChange={(e) => upload(e.target.files?.[0])}
          />
          {replacing && (
            <Button alignSelf="flex-start" size="sm" variant="ghost" onClick={() => setReplacing(false)}>
              Keep the current template
            </Button>
          )}
        </Stack>
      )}

      {importTemplate.isPending && <Text color="fg.muted">Reading the workbook…</Text>}
      {error && <ProblemAlert title="That file could not be imported" lines={lines} />}

      <StepNav
        onNext={onNext}
        nextDisabled={template === undefined || replacing}
        hint="Import a template to continue."
      />
    </Stack>
  );
}
