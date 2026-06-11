-- Add cost_centre (client name) to consultant_documents so
-- _document_exception_flags can look up the client profile
-- per consultant without joining back to the CSI payload.
-- Run against DATABASE_URL like the other db/*.sql files.

alter table consultant_documents
    add column if not exists cost_centre varchar(200);

create index if not exists idx_consultant_docs_cost_centre
    on consultant_documents (cost_centre);
