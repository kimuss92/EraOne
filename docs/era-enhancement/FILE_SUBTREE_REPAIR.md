# File-search subtree repair — 2026-10-13

## Approved scope and design

The user authorized continuing Phase 6. The preceding file-awareness increment
explicitly left directory descendant index repair open. Renaming a folder changed
only its own cached path; deleting it could leave indexed children at old paths.
Open-history anchors could also retain obsolete locations.

Repair the existing SQLite index on watcher events, rather than add another
watcher or recursively rescan the filesystem. Folder move/delete updates existing
descendant metadata and open-history paths in one transaction. A dedicated writer
connection prevents other shared-connection commits from splitting that transaction.
Path-component boundaries protect similarly named neighboring folders; `%` and `_`
in names are literal because the predicate does not use SQL LIKE.

Directory creation adds its own entry. Case-only renames and repeated parent/child
events are supported. A destination collision replaces obsolete cached metadata;
the moved source's cached open history wins at that exact destination. This updates
the database only; no production file is moved or deleted by this code.

## Related defects found and repaired

An overlapping-root build reproduced a SQLite FTS integrity failure. INSERT OR
REPLACE did not run the old row's delete trigger under the connection's normal
settings. Rebuild batches and incremental additions now use conflict updates so
the existing FTS update triggers run. Overlapping roots keep the highest priority
already observed. The subtree transaction enables recursive delete triggers for
its own replacement collisions, without changing the shared connection's settings.
No automatic rebuild or destructive migration of the user's existing database
was performed; previously corrupted cached indexes may still need an ordinary rebuild.

The first native Windows fixture exposed a second issue: watchdog emitted
deleted directories with is_directory=False once Windows could no longer stat
them. Indexed file rows were removed but descendant open-history entries remained.
The handler now recovers the known directory type from the index before dispatch
and sends that corrected type to file-awareness subscribers. An unknown deleted
path is not classified by its name.

## Verification and transparency

Initial simulated event checks passed. The overlapping-root FTS regression then
failed as expected, and the first native deletion test failed. Both causes were
investigated and repaired, rather than hidden by extending timeouts or retries.
The subsequent focused run passed 150 tests, including native Windows nested-folder
rename/delete, search and FTS integrity, history relocation, deletion boundaries,
rollback on a forced failure, destination conflicts, and the existing awareness/HUD
regressions. Ruff and compilation checks passed. Gate totals are in PHASE6_PROGRESS.md.

All actual create/rename/delete operations used a verified disposable TEMP fixture.
The native test checks unaffected neighboring files and observer shutdown. No
running Era session was restarted and no user file, device or media state changed.

## Remaining limitations

No new recursive scan is performed. Moving a previously unindexed tree into scope
still depends on native child events or the existing rebuild mechanism to discover
unknown descendants. Event loss, permissions and moves outside configured roots
remain coverage limits. This patch repairs known indexed descendants; it does not
claim perfect all-drive knowledge. Existing directory-skips and scope rules remain.

Live HUD/broad-folder acceptance, the search-summary/cancellation test and acoustic
glasses checks remain pending. Phase 6 still requires device awareness and broader
task recovery; native Telegram calls remain deferred until the separate account
is prepared.
