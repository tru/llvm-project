# fastbuild-scan-deps status

As of 2026-09-26. Branch `fastbuild-scan-deps-main`, based on LLVM `main` at `5445a8893512`.

- `design.md`: approved spec (C API, caching, cl translation, invalidation, crash policy).
- `plan.md`: approved 8-task implementation plan, with full code and tests. Its Environment section has the build and test scripts.

The performance targets assume the extension-less negative-stat caching patch to `clang/lib/DependencyScanning/DependencyScanningService.cpp` (local branch `scan-deps-extensionless-negative-stats`). It is not on this branch. Without it, a cold scan is roughly 27s, not 7.5s.

## Progress

| Task | State |
|---|---|
| 1 Scaffolding | Implemented, commit `c865c0c66bb7`. Tests pass. Task review not run yet. |
| 2 ConcurrentTable | Not started |
| 3 CommandAdapter | Not started |
| 4 Scan path + C API + `fbsd-cli run` | Not started |
| 5 Memo | Not started |
| 6 Invalidation | Not started |
| 7 Crash handling | Not started |
| 8 Bench + golden comparison | Not started |

Task 1 differs from the plan in two ways, both required by this tree:

- `PARTIAL_SOURCES_INTENDED` on both `add_llvm_library` calls in `clang/tools/fastbuild-scan-deps/CMakeLists.txt`. Keep it when adding sources.
- `check.bat` also builds `llvm-config`; lit won't start without it.

## Resuming

1. Recreate `build.bat` and `check.bat` from `plan.md`'s Environment section, with this machine's VS setup script, host clang-cl/lld-link, and build dir. Add `llvm-config` to `check.bat`'s targets.
2. Build and run the existing tests: `check.bat <repo>\clang\test\FastbuildScanDeps`, then `FastbuildScanDepsTests.exe` from the build tree.
3. Review Task 1 (`5445a8893512..c865c0c66bb7`) against `plan.md` Task 1, then do Tasks 2–8 in order. Each task is test-first and ends in its own commit.

Task 8 needs cl and clang-cl compile databases (`-compdb` exports) for a large real project.
