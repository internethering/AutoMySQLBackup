# AutoMySQLBackup

Automated MySQL/MariaDB backup tool with daily, weekly, and monthly rotation,
differential backups, optional compression and encryption, and email notification.

Originally a bash script (v1.0–3.0, 2002–2011). Rewritten in Python for v4.0.

## Requirements

- Python 3.11+
- PyYAML (`dev-python/pyyaml` or `pip install pyyaml`)
- `mysqldump` / `mysql` / `mysqlshow` (or MariaDB equivalents)
- Optional: `pigz`, `pbzip2` for multicore compression
- Optional: `openssl` for encryption
- Optional: `mail` / `mutt` for email notification
- Optional: `patch` for differential backup recovery

## Installation

```sh
# copy the package
cp -r automysqlbackup /usr/lib/python3/dist-packages/

# install the entry point
install -m 755 automysqlbackup/__main__.py /usr/local/bin/automysqlbackup

# create the config directory and copy the template
install -d /etc/automysqlbackup
cp automysqlbackup.yaml /etc/automysqlbackup/automysqlbackup.yaml
```

Edit `/etc/automysqlbackup/automysqlbackup.yaml` to set your credentials and options.

Alternatively, run directly from the source tree:

```sh
python3 -m automysqlbackup [options]
```

## Configuration

All settings live in `/etc/automysqlbackup/automysqlbackup.yaml`. Every key is
optional — unset keys fall back to the built-in defaults. A fully annotated
template is included as `automysqlbackup.yaml` in this repository.

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
```

The tool is safe to run multiple times on the same day: if a backup for the
current period already exists it is skipped.

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

When differential mode is active, `rotation.daily` is automatically raised to a
minimum of 21 days to ensure master files outlive their dependents.

## Restoring a full backup

```sh
# decompress
gunzip backup.sql.gz          # or: bunzip2 backup.sql.bz2

# decrypt (if encryption was used)
openssl enc -aes-256-cbc -d \
  -in backup.sql.gz.enc \
  -out backup.sql.gz \
  -pass pass:YOUR_PASSWORD

# restore
mysql --user=root --host=dbserver mydb < backup.sql
```

## Security

Credentials are never passed on the command line. A temporary `my.cnf` file
(`chmod 0600`) is created for each backup run and removed immediately after.
MySQL's `--defaults-extra-file` mechanism is used to pass it to all subprocesses.

## Running tests

```sh
python3 -m unittest discover -s tests -t . -v
```

## License

GNU General Public License v3 or later. See [LICENSE](LICENSE).
