# AutoMySQLBackup

Automated MySQL/MariaDB backup tool with daily, weekly, and monthly rotation,
differential backups, optional compression and encryption, and email notification.

Originally a bash script (v1.0–3.0, 2002–2011). Rewritten in Python for v4.0 with Antrophic Claude.

## Requirements

- Python 3.11+
- PyYAML (`dev-python/pyyaml` or `pip install pyyaml`)
- `mysqldump` / `mysql` / `mysqlshow` (or MariaDB equivalents)
- Optional: `pigz`, `pbzip2` for multicore compression
- Optional: `openssl` for encryption
- Optional: `mail` / `mutt` for email notification
- Optional: `diff` and `patch` for differential backups and their recovery
- Optional: `uuencode`-capable mail client or `mutt` for mailing backup files

## Installation

AutoMySQLBackup is built with setuptools. Build the packages once:

```sh
pip install build
python3 -m build            # creates dist/automysqlbackup-<version>.tar.gz and .whl
```

Then install the wheel on each server — into its own virtual environment
(recommended), with pipx, or directly from the source tree with `pip install .`:

```sh
python3 -m venv /opt/automysqlbackup
/opt/automysqlbackup/bin/pip install dist/automysqlbackup-*.whl
ln -s /opt/automysqlbackup/bin/automysqlbackup /usr/local/bin/automysqlbackup
```

Create the configuration from the template shipped with the package:

```sh
install -d -m 700 /etc/automysqlbackup
automysqlbackup --print-config > /etc/automysqlbackup/automysqlbackup.yaml
chmod 600 /etc/automysqlbackup/automysqlbackup.yaml
```

Edit `/etc/automysqlbackup/automysqlbackup.yaml` to set your credentials and options.
Put passwords in quotes: YAML turns unquoted values such as `012345` into
numbers (here: octal 5349) before the program ever sees them.

After installation the tool can also be started as `python3 -m automysqlbackup`.

## Configuration

All settings live in `/etc/automysqlbackup/automysqlbackup.yaml`. Every key is
optional — unset keys fall back to the built-in defaults. A fully annotated
template is printed by `automysqlbackup --print-config` (source:
`src/automysqlbackup.yaml`).

### Minimal example

```yaml
mysql:
  username: backupuser
  password: s3cr3t
  host: db.example.com

backup:
  dir: /var/backup/db

databases:
  exclude:
    - information_schema
    - performance_schema
```

### Key sections

| Section        | What it controls                                                          |
|----------------|---------------------------------------------------------------------------|
| `binaries`     | Paths to `mysql`, `mysqldump`, `mysqlshow` (or MariaDB equivalents)      |
| `mysql`        | Connection: host, port, credentials, SSL, MySQL 8 mode                    |
| `backup`       | Backup directory, local files to archive                                  |
| `schedule`     | Day-of-month for monthly (`do_monthly`), ISO weekday for weekly (`do_weekly`) |
| `rotation`     | Maximum age in days before a backup file is deleted                       |
| `databases`    | Include/exclude lists; separate monthly database list                     |
| `tables`       | Per-table exclusions, supports `db.prefix*` wildcards                     |
| `dump`         | Single-transaction, master-data, separate dirs, full schema, differential |
| `compression`  | `gzip` or `bzip2`; multicore via `pigz`/`pbzip2`                         |
| `latest`       | Keep a hardlinked copy in `latest/` at zero extra disk cost               |
| `encryption`   | openssl AES-256-CBC post-compression encryption                           |
| `notification` | `stdout`, `log`, `quiet`, or `files` mode; email address                  |
| `hooks`        | Shell commands to run before/after the backup                             |
| `runtime`      | `dryrun` and `debug` flags                                                |

Pass an additional config file with `-c` to layer settings on top of the global
config — useful for backing up multiple hosts from one machine.

## Usage

```
automysqlbackup [options]

  -c FILE   Optional config file (layered on top of global config)
  -b        Run backup (default)
  -l        Interactive differential backup manager
  -n        Dry run — show what would be done without making changes
  -v        Verbose output
  -d        Debug output
  --print-config  Print the annotated configuration template
  -V        Show the version
```

Weekly and monthly backups are created at most once per day: if a finished
backup for the current period already exists it is skipped. Every run creates a
new daily backup. Dumps are written to a `.part` file first and renamed only on
success, so a failed or interrupted dump is retried by the next run.

### Exit status

| Code | Meaning                                                  |
|------|----------------------------------------------------------|
| 0    | Backup completed without errors                          |
| 1    | At least one error was logged (e.g. a failed dump)       |
| 2    | Invalid configuration                                    |
| 130  | Interrupted                                              |

### Cron

```sh
# /etc/cron.daily/automysqlbackup
#!/bin/sh
automysqlbackup
find /var/backup/db -type f -exec chmod 400 {} \;
find /var/backup/db -type d -exec chmod 700 {} \;
```

## Backup structure

```
/var/backup/db/
  daily/
    mydb/
      daily_mydb_2024-03-15_00h00m_Friday_a3f9c21b.sql.gz
  weekly/
    mydb/
      weekly_mydb_2024-03-15_00h00m_11_d8e1b447.sql.gz
  monthly/
    mydb/
      monthly_mydb_2024-03-01_00h00m_March_7c2a9f83.sql.gz
  latest/          # hardlinks to today's files (zero extra disk space)
  fullschema/      # full schema dump (all databases, no data)
  status/          # mysqlshow --status output
  tmp/
```

## Differential backups

Enable with `dump: differential: true`. A full master backup is created on the
configured weekly day; on all other days a compressed unified diff is stored
instead. This drastically reduces daily backup size for large, slowly-changing
databases.

Recovery is interactive:

```sh
automysqlbackup -l
```

Or programmatically via `DiffRecovery.apply(master, diff)` which reconstructs
the full SQL file using `patch(1)`.

Diffs are computed with `diff -u` on plain SQL staged in `tmp/` (both the
master and the current dump are written there temporarily), so make sure that
directory has room for two uncompressed dumps.

When differential mode is active, `rotation.daily` is automatically raised to a
minimum of 21 days. In addition, a master is never rotated while any diff in the
Manifest still refers to it. Differential mode is skipped when encryption is on.

## Restoring a full backup

```sh
# decompress
gunzip backup.sql.gz          # or: bunzip2 backup.sql.bz2

# decrypt (if encryption was used); prompts for the password
openssl enc -d -aes-256-cbc -salt -pbkdf2 -iter 200000 \
  -in backup.sql.gz.enc \
  -out backup.sql.gz
# files encrypted before this change used the legacy key derivation:
#   openssl enc -d -aes-256-cbc -in backup.sql.gz.enc -out backup.sql.gz

# restore
mysql --user=root --host=dbserver mydb < backup.sql
```

## Security

Credentials are never passed on the command line. A temporary `my.cnf` file
(`chmod 0600`) is created for each backup run and removed immediately after.
MySQL's `--defaults-extra-file` mechanism is used to pass it to all subprocesses.
The encryption password is handed to `openssl` on stdin, never in its argument list.

## Running tests

```sh
python3 -m unittest discover -s tests -t . -v
```

The tests run from the source tree without installing the package; some of
them need `gzip`, `diff`, `patch` and `openssl` on the `PATH`.

## License

GNU General Public License v3 or later. See [LICENSE](LICENSE).
