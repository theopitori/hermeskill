# Dev DB bootstrap for the Windows machine (Postgres 18 already installed).
#
# Creates the `caspase` role + `caspase` database. Re-runnable. Requires
# `psql` on PATH and a Postgres superuser to authenticate (the script uses
# Windows-auth `-U postgres` by default).
#
# Usage:
#   .\deploy\dev-db-bootstrap.ps1                  # uses password 'caspase'
#   .\deploy\dev-db-bootstrap.ps1 -Password 'xyz'  # custom

param(
    [string]$Password = "caspase",
    [string]$SuperuserName = "postgres"
)

$ErrorActionPreference = "Stop"

$sql = @"
DO `$`$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'caspase') THEN
    CREATE ROLE caspase LOGIN PASSWORD '$Password';
  ELSE
    ALTER ROLE caspase WITH PASSWORD '$Password';
  END IF;
END
`$`$;
"@

Write-Output ">>> ensuring 'caspase' role"
$sql | & psql -U $SuperuserName -d postgres

Write-Output ">>> ensuring 'caspase' database"
$dbExists = & psql -U $SuperuserName -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='caspase'"
if (-not $dbExists) {
    & createdb -U $SuperuserName -O caspase caspase
}

Write-Output ">>> done. connection string:"
Write-Output "postgresql+psycopg://caspase:$Password@localhost:5432/caspase"
