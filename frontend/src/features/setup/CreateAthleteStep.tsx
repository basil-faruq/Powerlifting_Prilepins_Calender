import { Button, Field, Heading, Input, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useCreateAthlete } from "@/api/hooks/athletes";

/**
 * Before step 1 on a fresh database: there is nobody to attach maxes to yet. Not one
 * of the five steps. `POST /athletes` is get-or-create, so re-running this is safe.
 */
export function CreateAthleteStep() {
  const [name, setName] = useState("");
  const create = useCreateAthlete();
  const trimmed = name.trim();

  return (
    <Stack gap="4" maxW="md">
      <Heading size="lg">Welcome</Heading>
      <Text color="fg.muted">Who is training? This is only used to label the plan.</Text>
      <Field.Root>
        <Field.Label>Your name</Field.Label>
        <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoFocus />
      </Field.Root>
      <Button
        alignSelf="flex-start"
        onClick={() => create.mutate(trimmed)}
        disabled={trimmed === ""}
        loading={create.isPending}
      >
        Continue
      </Button>
    </Stack>
  );
}
