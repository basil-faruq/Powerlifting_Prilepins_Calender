import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../client";
import { keys } from "../keys";
import { unwrap } from "../unwrap";

export const templatesOptions = () =>
  queryOptions({
    queryKey: keys.templates.list(),
    queryFn: () => unwrap(api.GET("/templates")),
  });

export const templateOptions = (templateId: string) =>
  queryOptions({
    queryKey: keys.templates.detail(templateId),
    queryFn: () =>
      unwrap(api.GET("/templates/{template_id}", { params: { path: { template_id: templateId } } })),
  });

/** Every imported template, newest first, without their rows. */
export const useTemplates = () => useQuery(templatesOptions());

/** One template with all its entries. */
export const useTemplate = (templateId: string | null) =>
  useQuery({ ...templateOptions(templateId ?? ""), enabled: templateId !== null });

/**
 * Upload the authored workbook. `replace` is how a corrected spreadsheet gets past
 * the 409 a second import of the same name otherwise earns.
 */
export function useImportTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    // The wizard shows an import failure itself, with the cell references listed.
    meta: { silent: true },
    mutationFn: (args: { file: File; name?: string; replace?: boolean }) =>
      unwrap(
        api.POST("/templates/import", {
          params: { query: { name: args.name ?? "B1", replace: args.replace ?? false } },
          // The schema types a file as `string`; the serializer sends the real File.
          body: { file: args.file as unknown as string },
          bodySerializer: (body) => {
            const form = new FormData();
            form.append("file", body.file as unknown as Blob);
            return form;
          },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.templates.all }),
  });
}
