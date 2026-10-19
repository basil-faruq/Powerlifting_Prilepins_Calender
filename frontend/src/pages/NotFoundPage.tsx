import { Button } from "@chakra-ui/react";
import { Link } from "react-router";
import { EmptyState } from "@/components/states/EmptyState";

export function NotFoundPage() {
  return (
    <EmptyState
      title="Page not found"
      description="There is nothing at this address."
      action={
        <Button asChild variant="outline">
          <Link to="/">Go home</Link>
        </Button>
      }
    />
  );
}
