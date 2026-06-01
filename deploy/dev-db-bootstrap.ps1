# Dev DB bootstrap for the Windows machine (Postgres 18 already installed).
#
# Creates the `hermeskill` role + `hermeskill` database. Re-runnable. Requires
# `psql` on PATH and a Postgres superuser to authenticate (the script uses
# Windows-auth `-U postgres` by default).
#
# Usage:
#   .\deploy\dev-db-bootstrap.ps1                  # uses password 'hermeskill'
#   .\deploy\dev-db-bootstrap.ps1 -Password 'xyz'  # custom

param(
    [string]$Password = "hermeskill",
    [string]$SuperuserName = "postgres"
)

$ErrorActionPreference = "Stop"

$sql = @"
DO `$`$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hermeskill') THEN
    CREATE ROLE hermeskill LOGIN PASSWORD '$Password';
  ELSE
    ALTER ROLE hermeskill WITH PASSWORD '$Password';
  END IF;
END
`$`$;
"@

Write-Output ">>> ensuring 'hermeskill' role"
$sql | & psql -U $SuperuserName -d postgres

Write-Output ">>> ensuring 'hermeskill' database"
$dbExists = & psql -U $SuperuserName -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='hermeskill'"
if (-not $dbExists) {
    & createdb -U $SuperuserName -O hermeskill hermeskill
}

Write-Output ">>> done. connection string:"
Write-Output "postgresql+psycopg://hermeskill:$Password@localhost:5432/hermeskill"
