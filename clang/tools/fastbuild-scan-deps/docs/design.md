# fastbuild-scan-deps design

2026-09-25. Implements fix 4 of [FASTBuild distribution improvements](FASTBuild%20distribution%20improvements.md).

## Goal

A DLL, `fastbuild-scan-deps.dll`, built from LLVM, that FASTBuild loads once per build to get the exact set of files each TU depends on. It is the input to sandboxed distribution and later to caching, so it must be correct first and fast second.

- One call scans one TU from its command line. No compilation database.
- Output: the de-duplicated real paths of every file the TU reads, source first.
- The DLL stays loaded for the build, so the stat/file cache is shared by all scans, and a repeat request for the same command is a memo lookup.
- Plain includes only: no C++20 or clang modules.

Out of scope for v1: modules, replacing clang's shard mutexes in the FS cache, a persistent stat database, and the FASTBuild-side integration (separate spec).

## Layout

- `clang/tools/fastbuild-scan-deps/`: `SHARED` CMake target with clang and LLVM linked statically, so the DLL is self-contained. Exports only `fbsd_*` via a `.def` file.
- `fbsd.h`: the public header, C89, no C++ types.
- `fbsd-cli.exe`: in the same directory, drives the DLL through the public API only, for tests and benchmarks.

Components:

| Unit | Responsibility |
|---|---|
| `FbsdContext` | Owns one `DependencyScanningService` (negative-stat caching on), the intern table, the memo, and the poisoned flag. |
| Thread-local worker | A `DependencyScanningWorker` per calling thread, created lazily, sharing the context's service. |
| `CommandAdapter` | Response-file expansion, driver detection, cl translation, PCH stripping, memo key. |
| `InternConsumer` | `DependencyConsumer` that maps each reported file to an interned real path and de-duplicates within the TU. |
| `ConcurrentTable` | The lock-free-read hash table used by the memo and the intern table. |

## C API

```c
#define FBSD_API_VERSION 1

typedef struct fbsd_ctx fbsd_ctx;

typedef struct {
  uint32_t struct_size;
  uint32_t flags;
  uint32_t expected_tus;      /* sizing hint; 0 = default */
} fbsd_options;

enum { FBSD_RESULT_INEXACT = 1u << 0, FBSD_RESULT_MEMO_HIT = 1u << 1 };

typedef struct {
  const char *const *files;   /* files[0] is the source; owned by ctx */
  uint32_t count;
  uint32_t flags;
} fbsd_result;

enum {
  FBSD_OK = 0,
  FBSD_E_ARGS,                /* unparseable command line */
  FBSD_E_DRIVER,              /* argv[0] is not a known driver */
  FBSD_E_SCAN,                /* clang error, e.g. missing include */
  FBSD_E_CRASH,               /* scan crashed; ctx is poisoned */
};

uint32_t  fbsd_api_version(void);
fbsd_ctx *fbsd_create(const fbsd_options *opts);
void      fbsd_destroy(fbsd_ctx *ctx);

int fbsd_scan(fbsd_ctx *ctx, const char *cwd,
              const char *const *argv, uint32_t argc,
              fbsd_result *out, const char **diag);

uint32_t fbsd_invalidate(fbsd_ctx *ctx, const char *const *paths, uint32_t n);

void fbsd_stats(fbsd_ctx *ctx, fbsd_stats_out *out);
```

Rules:

- All strings are UTF-8.
- `fbsd_scan` and `fbsd_invalidate` are thread-safe. `fbsd_create` and `fbsd_destroy` are not.
- `files` and every string in it stay valid until `fbsd_destroy`. The caller frees nothing.
- `*diag` is per-thread and valid until that thread's next call. It is set on every non-OK return.
- Options and stats structs start with `struct_size`, so fields can be appended without a version bump. Any other ABI change bumps `FBSD_API_VERSION`. FASTBuild checks `fbsd_api_version()` after `LoadLibrary` and refuses a mismatch.
- `fbsd_stats_out` holds: scans, memo hits, errors by code, invalidations, entries dropped, service resets, interned files, memo entries, arena bytes, process private bytes (clang's cache allocators aren't reachable), a scan-time histogram, and a memo-hit latency histogram.

## Scan flow

1. If the context is poisoned, return `FBSD_E_CRASH`.
2. `CommandAdapter` builds the adapted command line and serialises it with `cwd` into the memo key.
3. Look up the memo. On a hit, return the stored result with `FBSD_RESULT_MEMO_HIT`.
4. Get or create this thread's worker, and run `computeDependencies` inside `llvm::CrashRecoveryContext` with an `InternConsumer`.
5. On success, publish the result to the memo (first insert wins) and return it.

## CommandAdapter

Every command line:

- Expand `@file` response files through the cached FS. The expanded arguments are what gets keyed.
- Drop flags that only affect output or diagnostics: `/showIncludes`, `/sourceDependencies`, `/MP`, `/FS`.
- Drop `/Yc`, `/Yu` and `/Fp`, and keep `/FI`. The PCH header is then scanned as an ordinary include, so its transitive headers are in the result, and scanning never needs a `.pch`.

Driver detection is by the case-insensitive basename of `argv[0]`, with `.exe` stripped:

| Driver | Handling | Exact |
|---|---|---|
| `clang-cl`, `clang`, `clang++` | Passed through. Resource dir comes from the compiler's own location (`<bin>/../lib/clang/<major>`), cached per compiler path. | Yes, unless the compiler's clang major version differs from the DLL's |
| `cl` | Translated, see below | No, the result carries `FBSD_RESULT_INEXACT` |
| other | `FBSD_E_DRIVER` | — |

cl translation, a bridge until the move to clang-cl:

- Read the file-version resource of `cl.exe` once per path, and add `-fms-compatibility-version=<major>.<minor>.<build>` so `_MSC_VER` and `_MSC_FULL_VER` match.
- Add `-nobuiltininc`, so compiler-provided headers such as `intrin.h` resolve through the command line's include paths, as they do for cl.
- Add `-nostdlibinc` to disable clang's MSVC and Windows SDK auto-detection. Unless the line has `/X`, also add `/external:env:INCLUDE` and `/external:env:EXTERNAL_INCLUDE` to restore cl's environment search. The search path is then only the command line's `/I`, `/external:I` and the `INCLUDE` variables.
- Unknown cl flags are warnings in clang-cl and don't fail the scan. Only error diagnostics do.

## Concurrent tables

The memo and the intern table share one design, optimised for many readers and rare writers.

- Open addressing over an array of atomic pointers to immutable entries.
- Lookup: acquire-load the table pointer, probe, acquire-load the slot, compare. No locks, no reference counts.
- Insert: build the entry, then CAS it into an empty slot. If a concurrent insert of an equal key wins, discard the local entry and use the winner's.
- Growth: under a writer mutex, allocate a table twice the size, rehash, and release-store the new table pointer. Old tables and entries are kept until `fbsd_destroy`, so a reader still probing an old table is always safe. `expected_tus` sizes the memo so a normal build never grows it.
- Entries, interned strings and result arrays are allocated from per-thread bump arenas that are freed only by `fbsd_destroy`.

Memo:

- The key is `cwd`, `INCLUDE`, `EXTERNAL_INCLUDE`, and the argv as given after response-file expansion. It is taken before adaptation, which is a pure function of these, so a hit skips the adapter. Response files are read through the real file system on every call, not the scan cache, because FASTBuild writes them during the build.
- The table indexes the key by xxh3-128, but an entry stores the full key bytes, and a hit requires a byte-equal compare. A hash collision cannot return another TU's dependencies.
- The value is `fbsd_result`: a pointer array into the intern table, and flags.
- Errors are not memoised.

Intern table:

- Key: file unique ID (volume serial plus 128-bit file ID).
- Value: an interned real path.
- On a miss, the file is opened once and its real path is taken from `GetFinalPathNameByHandleW`, with junctions and subst drives resolved and the case as stored on disk. This cost is paid once per unique file per build.

## InternConsumer

`handleFileDependency(name)`:

1. Stat `name` through the worker's caching FS to get the unique ID. This is a cache hit, since the scanner has just read the file.
2. Look the ID up in the intern table, inserting on a miss.
3. Skip it if its intern index is already in this TU's seen set. The seen set is a per-thread generation-stamped array indexed by intern index, so clearing it between scans is O(1).
4. Append the path to the per-thread result buffer. The source file is placed first.

On success the buffer is copied into the arena as the result array.

## Invalidation

The expected mode is that FASTBuild finishes all code generation before the first scan, so `fbsd_invalidate` should always return 0. It exists to prove that.

- Take the memo's writer mutex. Map each path to its unique ID if it exists. Drop every memo entry whose file list contains an invalidated ID; this is a linear pass, fine for a rare call.
- Clang's shared FS cache cannot evict single entries. A small accessor, `DependencyScanningFilesystemSharedCache::forEachCachedFilename`, lists the cached spellings, which are compared after `..` removal, lowercasing and backslash normalisation. If any invalidated path is present, positive or negative, install a fresh `DependencyScanningService` and clear the whole memo, since memo entries don't record the misses they depended on. Workers pick up the new service on their next scan.
- Return the number of memo entries and FS-cache entries dropped.

## Errors and crashes

- `FBSD_E_ARGS`, `FBSD_E_DRIVER`, `FBSD_E_SCAN`: that job fails with `*diag`, like a compile error. With sandboxed distribution there is no fallback to local preprocessing.
- `FBSD_E_CRASH`: `CrashRecoveryContext` catches the crash only to report it. `*diag` names the source, the full command line and the crash description. The context is then poisoned, and FASTBuild stops the build.

## Testing

- Lit tests in `clang/test/fastbuild-scan-deps/` via `fbsd-cli`: quoted and angled includes, `/FI`, `#include_next`, `__has_include`, PCH stripping, de-dup across case, `..` and junction spellings, memo hits, `invalidate` counts, and every error code. cl translation tests are skipped when `cl.exe` isn't found.
- Unit tests in `clang/unittests/FastbuildScanDeps/`: concurrent insert/lookup storm, growth under live readers, forced-collision keys, thread-local worker lifecycle, poisoning.
- Golden comparison on the 2945 TUs of the benchmark project, as sets after real-path normalisation:
  - clang-cl command lines against `clang-cl /showIncludes`: must match exactly.
  - cl command lines against `cl /showIncludes`: mismatches are reported, and the count decides whether the cl bridge is usable.

## Performance targets

On the 48-thread benchmark machine, full benchmark project, via `fbsd-cli --bench <compdb> -j48 --repeat 2`:

| Metric | Target |
|---|---|
| Cold scan of all TUs, 48 threads | ≤ 8s wall (`clang-scan-deps` with the same patches: 7.5s) |
| Memo hit | < 5 µs |
| Peak memory | Measured; decide on a cap after an Apps-sized scan |
