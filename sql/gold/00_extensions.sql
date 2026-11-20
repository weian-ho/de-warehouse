-- sql/gold/00_extensions.sql
-- T052 gold layer, file 1 of 4: what the other files need. Idempotent.
-- Applied by `make gold` (all four files in ONE transaction).

-- btree_gist lets a GiST index compare plain int/text with "=". Without it, the exclusion
-- constraint on dim_instrument fails: "no default operator class for access method gist".
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS gold;
