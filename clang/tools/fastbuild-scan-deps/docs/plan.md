# fastbuild-scan-deps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `fastbuild-scan-deps.dll`, a self-contained Windows DLL with a stable C API that returns the de-duplicated real paths of every file a TU depends on, with a build-lifetime stat cache and result memo.

**Architecture:** A static core library (`fbsdCore`) wraps clang's `DependencyScanningService`/`DependencyScanningWorker`: a `CommandAdapter` normalises cl/clang-cl/clang command lines, a thread-local worker scans on the caller's thread, an `InternConsumer` maps each dependency to an interned real path by file identity, and lock-free-read tables hold the intern table and the memo. The DLL is a thin C shim over `fbsdCore`; `fbsd-cli.exe` drives the DLL through `LoadLibrary` for lit tests and benchmarks.

**Tech Stack:** C++17 inside LLVM (`E:\code\llvm-project`, branch off `main`), clang DependencyScanning/Tooling libraries, LLVM Support, CMake + Ninja, clang-cl 23.1.1 + lld-link, lit/FileCheck, googletest.

**Spec:** `clang/tools/fastbuild-scan-deps/docs/design.md`

## Global Constraints

- Windows x64 only. Every new CMake directory is added under `if(WIN32)`.
- Public header `fbsd.h`: C only, no C++ types, `FBSD_API_VERSION 1`, UTF-8 strings.
- Exported symbols: only `fbsd_*`, via `fbsd.exports`.
- Clang and LLVM are linked statically into the DLL; static CRT (`CMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded`).
- Results (`files` and every string in it) stay valid until `fbsd_destroy`. The caller frees nothing.
- `*diag` is per-thread, valid until that thread's next call, set on every non-OK return.
- `fbsd_scan` and `fbsd_invalidate` are thread-safe. `fbsd_create` and `fbsd_destroy` are not.
- Read paths of the memo and intern table take no locks. Inserts may take a mutex.
- Memo hits require a byte-equal full-key compare; a hash match alone never returns a result.
- A crash poisons the context: every later call returns `FBSD_E_CRASH`, and FASTBuild stops the build.
- Code style: comment only true footguns, one line each. No history in comments.
- Performance targets on the 48-thread benchmark machine, all 2945 TUs of the benchmark project, 48 threads: cold scan ≤ 8s wall, memo hit < 5 µs.

## Deviations from the spec, decided while planning

- **Memo key is taken before adaptation**: cwd + `INCLUDE` + `EXTERNAL_INCLUDE` + the response-file-expanded argv as given. Adaptation is a pure function of these, so a hit skips it. Response files are read through the real file system every call, never through the scan cache, because FASTBuild writes them during the build.
- **cl `INCLUDE` handling**: translated cl lines get `-nostdlibinc` (disables MSVC/SDK auto-detection) plus `/external:env:INCLUDE` and `/external:env:EXTERNAL_INCLUDE` (restores cl's env search), unless the line has `/X`.
- **FS cache bytes** is reported as process private bytes. Clang's cache allocators are not reachable.
- **Invalidation needs one clang change**: `DependencyScanningFilesystemSharedCache::forEachCachedFilename`, to find cached spellings of an invalidated path.

## Review Focus

1. **Response file with a relative path** (`@args.rsp`) must resolve against the scan's `cwd`, not the process cwd. Test in Task 3.
2. **`__has_include(<x.h>)` for an existing header that is never included** must list `x.h`. A worker without it would compile a different branch. Test in Task 4.
3. **Non-ASCII paths** in cwd, argv and results must round-trip as UTF-8. Test in Task 4.
4. **Many threads scanning the same TU at the same moment** must all get the same result pointer, and there must be one memo entry. Test in Task 5.
5. **A scan after a failed scan on the same thread** must succeed, with no stale diag or leftover files. Test in Task 4.

---

## Environment

All build commands run from Git Bash via `cmd //c`. Build dir: `E:\code\llvm-project-build\fbsd`. Scripts and logs: `D:\scratch\fbsd\`.

`D:\scratch\fbsd\build.bat` (created in Task 1):

```bat
@echo off
call <VS x64 setup script> >nul || exit /b 1
set LLVMBIN=<LLVM 23 bin dir>
set B=E:\code\llvm-project-build\fbsd
if not exist %B%\build.ninja (
cmake -G Ninja -S E:\code\llvm-project\llvm -B %B% ^
  -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON ^
  -DLLVM_ENABLE_PROJECTS=clang -DLLVM_TARGETS_TO_BUILD=X86 ^
  -DLLVM_ENABLE_RPMALLOC=ON -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded ^
  -DCMAKE_C_COMPILER=%LLVMBIN%\clang-cl.exe -DCMAKE_CXX_COMPILER=%LLVMBIN%\clang-cl.exe ^
  -DCMAKE_LINKER=%LLVMBIN%\lld-link.exe -DLLVM_ENABLE_LLD=ON ^
  -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF || exit /b 1
)
ninja -C %B% %*
```

`D:\scratch\fbsd\check.bat` (created in Task 1): builds everything the tests need, then runs lit on the given paths.

```bat
@echo off
call D:\scratch\fbsd\build.bat fastbuild-scan-deps fbsd-cli clang FileCheck split-file not count FastbuildScanDepsTests || exit /b 1
python E:\code\llvm-project-build\fbsd\bin\llvm-lit.py -sv %*
```

Unit tests: `E:\code\llvm-project-build\fbsd\tools\clang\unittests\FastbuildScanDeps\FastbuildScanDepsTests.exe`. If the binary is elsewhere, find it with `find /e/code/llvm-project-build/fbsd -name FastbuildScanDepsTests.exe`.

Test inputs for Task 8, from earlier benchmarking:

- cl compdb: `<compdb dir>\compile_commands.json`
- clang-cl compdb: `<compdb dir>\compdb_clang\compile_commands.json`

Both have 2945 entries with `directory`, `file`, `arguments`.

## File structure

```
clang/tools/fastbuild-scan-deps/
  CMakeLists.txt           fbsdCore static lib + fastbuild-scan-deps DLL
  include/fbsd.h           public C API
  fbsd.exports             exported symbols, one per line
  fbsd.cpp                 C shims over fbsd::Context
  ConcurrentTable.h        lock-free-read hash table (header only)
  CommandAdapter.h/.cpp    driver detection, rsp expansion, flag stripping, cl translation, memo key
  InternTable.h/.cpp       UniqueID -> interned real path
  InternConsumer.h/.cpp    DependencyConsumer that interns and de-dups
  Context.h/.cpp           FbsdContext: service, thread states, memo, invalidation, stats, crash
  cli/CMakeLists.txt
  cli/FbsdCli.cpp          fbsd-cli: version / run / bench
  utils/fbsd_golden.py     golden comparison against /showIncludes
clang/unittests/FastbuildScanDeps/
  CMakeLists.txt
  ConcurrentTableTest.cpp
  CommandAdapterTest.cpp
  ContextTest.cpp
clang/test/FastbuildScanDeps/
  lit.local.cfg
  *.c                      lit tests (split-file inputs + JSON-lines scripts)
```

Modified: `clang/tools/CMakeLists.txt`, `clang/unittests/CMakeLists.txt`, `clang/test/CMakeLists.txt`, `clang/test/lit.cfg.py`, `clang/include/clang/DependencyScanning/DependencyScanningFilesystem.h`, `clang/lib/DependencyScanning/DependencyScanningFilesystem.cpp`.

---

### Task 1: Scaffolding — DLL, CLI, lit and unittest wiring

**Files:**
- Create: `clang/tools/fastbuild-scan-deps/CMakeLists.txt`, `include/fbsd.h`, `fbsd.exports`, `fbsd.cpp`, `cli/CMakeLists.txt`, `cli/FbsdCli.cpp`
- Create: `clang/unittests/FastbuildScanDeps/CMakeLists.txt`, `clang/unittests/FastbuildScanDeps/SmokeTest.cpp`
- Create: `clang/test/FastbuildScanDeps/lit.local.cfg`, `clang/test/FastbuildScanDeps/version.c`
- Create: `D:\scratch\fbsd\build.bat`, `D:\scratch\fbsd\check.bat` (contents in Environment)
- Modify: `clang/tools/CMakeLists.txt`, `clang/unittests/CMakeLists.txt`, `clang/test/CMakeLists.txt:~103`, `clang/test/lit.cfg.py:~130`

**Interfaces:**
- Produces: `fbsd.h` (complete, all later tasks implement against it); target `fbsdCore` (static, PUBLIC include dirs `include/` and the tool dir); target `fastbuild-scan-deps` (DLL in `bin/`); `fbsd-cli` with `version` subcommand; the `Api` struct + `loadApi()` in `FbsdCli.cpp`.

- [ ] **Step 1: Branch**

```bash
cd /e/code/llvm-project && git checkout -b fastbuild-scan-deps main
```

- [ ] **Step 2: Write the scripts** `build.bat` and `check.bat` exactly as in Environment.

- [ ] **Step 3: Write the public header** `clang/tools/fastbuild-scan-deps/include/fbsd.h`

```c
#ifndef FBSD_H
#define FBSD_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FBSD_API_VERSION 1

typedef struct fbsd_ctx fbsd_ctx;

typedef struct fbsd_options {
  uint32_t struct_size;
  uint32_t flags;
  uint32_t expected_tus; /* sizing hint; 0 = default */
} fbsd_options;

enum { FBSD_RESULT_INEXACT = 1u << 0, FBSD_RESULT_MEMO_HIT = 1u << 1 };

typedef struct fbsd_result {
  const char *const *files; /* files[0] is the source; owned by ctx */
  uint32_t count;
  uint32_t flags;
} fbsd_result;

enum {
  FBSD_OK = 0,
  FBSD_E_ARGS = 1,
  FBSD_E_DRIVER = 2,
  FBSD_E_SCAN = 3,
  FBSD_E_CRASH = 4,
  FBSD_E_COUNT = 5
};

#define FBSD_HIST_BUCKETS 40 /* bucket i: [2^i, 2^(i+1)) ns */

typedef struct fbsd_stats_out {
  uint32_t struct_size;
  uint32_t reserved;
  uint64_t scans;
  uint64_t memo_hits;
  uint64_t errors[FBSD_E_COUNT];
  uint64_t invalidate_calls;
  uint64_t invalidated_entries;
  uint64_t service_resets;
  uint64_t interned_files;
  uint64_t memo_entries;
  uint64_t arena_bytes;
  uint64_t process_private_bytes;
  uint64_t scan_ns[FBSD_HIST_BUCKETS];
  uint64_t memo_hit_ns[FBSD_HIST_BUCKETS];
} fbsd_stats_out;

uint32_t fbsd_api_version(void);
fbsd_ctx *fbsd_create(const fbsd_options *opts);
void fbsd_destroy(fbsd_ctx *ctx);
int fbsd_scan(fbsd_ctx *ctx, const char *cwd, const char *const *argv,
              uint32_t argc, fbsd_result *out, const char **diag);
uint32_t fbsd_invalidate(fbsd_ctx *ctx, const char *const *paths, uint32_t n);
void fbsd_stats(fbsd_ctx *ctx, fbsd_stats_out *out);

#ifdef __cplusplus
}
#endif

#endif
```

- [ ] **Step 4: Write `fbsd.exports`** (Task 4 adds the rest)

```
fbsd_api_version
```

- [ ] **Step 5: Write `fbsd.cpp`**

```cpp
#include "fbsd.h"

extern "C" uint32_t fbsd_api_version(void) { return FBSD_API_VERSION; }
```

- [ ] **Step 6: Write `clang/tools/fastbuild-scan-deps/CMakeLists.txt`**

`fbsdCore` starts with one placeholder source so the target exists. Task 2 replaces `CoreStub.cpp` with real sources.

```cmake
add_llvm_library(fbsdCore STATIC BUILDTREE_ONLY
  CoreStub.cpp

  LINK_COMPONENTS
  ${LLVM_TARGETS_TO_BUILD}
  Option
  Support
  TargetParser
  )
clang_target_link_libraries(fbsdCore
  PUBLIC
  clangBasic
  clangDependencyScanning
  clangDriver
  clangFrontend
  clangTooling
  )
target_link_libraries(fbsdCore PRIVATE version)
target_include_directories(fbsdCore PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  )

set(LLVM_EXPORTED_SYMBOL_FILE ${CMAKE_CURRENT_SOURCE_DIR}/fbsd.exports)
add_llvm_library(fastbuild-scan-deps SHARED BUILDTREE_ONLY
  fbsd.cpp

  LINK_LIBS
  fbsdCore
  )
set(LLVM_EXPORTED_SYMBOL_FILE)

add_subdirectory(cli)
```

Create `CoreStub.cpp` containing only `namespace fbsd {}`.

- [ ] **Step 7: Write the CLI** `cli/CMakeLists.txt`

```cmake
set(LLVM_LINK_COMPONENTS Support)
add_clang_tool(fbsd-cli FbsdCli.cpp)
target_include_directories(fbsd-cli PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../include)
add_dependencies(fbsd-cli fastbuild-scan-deps)
```

`cli/FbsdCli.cpp`:

```cpp
#include "fbsd.h"
#include "llvm/ADT/SmallString.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/Support/ConvertUTF.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Program.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/Windows/WindowsSupport.h"
#include <string>
#include <vector>

using namespace llvm;

namespace {
struct Api {
  uint32_t (*api_version)();
};

bool loadApi(StringRef DllPath, Api &A, std::string &Err) {
  SmallVector<wchar_t, 260> W;
  if (sys::windows::UTF8ToUTF16(DllPath, W)) {
    Err = "bad dll path";
    return false;
  }
  HMODULE M = LoadLibraryW(W.data());
  if (!M) {
    Err = ("LoadLibrary failed for " + DllPath).str();
    return false;
  }
  auto Get = [&](const char *Name) -> FARPROC {
    FARPROC P = GetProcAddress(M, Name);
    if (!P)
      Err = std::string("missing export ") + Name;
    return P;
  };
  A.api_version = reinterpret_cast<uint32_t (*)()>(Get("fbsd_api_version"));
  return Err.empty();
}

std::string defaultDllPath(const char *Argv0) {
  std::string Exe = sys::fs::getMainExecutable(Argv0, (void *)&defaultDllPath);
  SmallString<260> P(sys::path::parent_path(Exe));
  sys::path::append(P, "fastbuild-scan-deps.dll");
  return std::string(P);
}

int cmdVersion(const Api &A) {
  outs() << "api " << A.api_version() << "\n";
  return 0;
}
} // namespace

int main(int argc, char **argv) {
  InitLLVM X(argc, argv);
  if (argc < 2) {
    errs() << "usage: fbsd-cli [--dll PATH] version|run|bench ...\n";
    return 2;
  }
  std::vector<StringRef> Args(argv + 1, argv + argc);
  std::string Dll = defaultDllPath(argv[0]);
  if (Args.size() >= 2 && Args[0] == "--dll") {
    Dll = Args[1].str();
    Args.erase(Args.begin(), Args.begin() + 2);
  }
  Api A;
  std::string Err;
  if (!loadApi(Dll, A, Err)) {
    errs() << "fbsd-cli: " << Err << "\n";
    return 2;
  }
  if (Args[0] == "version")
    return cmdVersion(A);
  errs() << "fbsd-cli: unknown command " << Args[0] << "\n";
  return 2;
}
```

- [ ] **Step 8: Register the tool directory** in `clang/tools/CMakeLists.txt`, after `add_clang_subdirectory(clang-scan-deps)`:

```cmake
if(WIN32)
  add_clang_subdirectory(fastbuild-scan-deps)
endif()
```

- [ ] **Step 9: Wire up lit.** In `clang/test/CMakeLists.txt`, after the `list(APPEND CLANG_TEST_DEPS` block that contains `clang-scan-deps` (line ~103), add:

```cmake
if(WIN32)
  list(APPEND CLANG_TEST_DEPS fastbuild-scan-deps fbsd-cli)
endif()
```

In `clang/test/lit.cfg.py`, add `"fbsd-cli",` to the `tools` list next to `"clang-scan-deps",` (line ~130).

`clang/test/FastbuildScanDeps/lit.local.cfg`:

```python
import os
import shutil

if "system-windows" not in config.available_features:
    config.unsupported = True

cl = os.environ.get("FBSD_TEST_CL") or shutil.which("cl")
if cl:
    config.available_features.add("fbsd-cl")
    config.substitutions.append(("%{cl}", cl))
```

- [ ] **Step 10: Write the first lit test** `clang/test/FastbuildScanDeps/version.c`

```c
// RUN: fbsd-cli version | FileCheck %s
// CHECK: api 1
```

- [ ] **Step 11: Wire up unit tests.** `clang/unittests/FastbuildScanDeps/CMakeLists.txt`:

```cmake
add_distinct_clang_unittest(FastbuildScanDepsTests
  SmokeTest.cpp

  LINK_LIBS
  fbsdCore
  LLVM_COMPONENTS
  Support
  )
```

`SmokeTest.cpp`:

```cpp
#include "fbsd.h"
#include "gtest/gtest.h"

TEST(FbsdSmoke, HeaderVersion) { EXPECT_EQ(FBSD_API_VERSION, 1); }
```

In `clang/unittests/CMakeLists.txt`, next to `add_subdirectory(DependencyScanning)`:

```cmake
if(WIN32)
  add_subdirectory(FastbuildScanDeps)
endif()
```

- [ ] **Step 12: Configure, build and run**

```bash
cmd //c 'D:\scratch\fbsd\check.bat E:\code\llvm-project\clang\test\FastbuildScanDeps' 2>&1 | tee /d/scratch/fbsd/task1.log | tail -20
/e/code/llvm-project-build/fbsd/tools/clang/unittests/FastbuildScanDeps/FastbuildScanDepsTests.exe
```

Expected: the first configure and build take a long time; then `version.c` PASS and `FbsdSmoke.HeaderVersion` PASS. Check that the DLL exports only `fbsd_api_version`:

```bash
llvm-readobj --coff-exports /e/code/llvm-project-build/fbsd/bin/fastbuild-scan-deps.dll | grep Name:
```

- [ ] **Step 13: Commit**

```bash
git add clang/tools/fastbuild-scan-deps clang/unittests/FastbuildScanDeps clang/test/FastbuildScanDeps clang/tools/CMakeLists.txt clang/unittests/CMakeLists.txt clang/test/CMakeLists.txt clang/test/lit.cfg.py
git commit -m "[fbsd] Scaffold fastbuild-scan-deps DLL, CLI and tests

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: ConcurrentTable

**Files:**
- Create: `clang/tools/fastbuild-scan-deps/ConcurrentTable.h`
- Create: `clang/unittests/FastbuildScanDeps/ConcurrentTableTest.cpp`
- Modify: `clang/unittests/FastbuildScanDeps/CMakeLists.txt` (add the test source)

**Interfaces:**
- Produces: `template <typename EntryT> class fbsd::ConcurrentTable`. `EntryT` must have a public `uint64_t Hash` member. Methods:
  - `explicit ConcurrentTable(size_t ExpectedEntries = 1024)`
  - `template <typename Pred> EntryT *find(uint64_t Hash, Pred Matches) const`: lock-free
  - `template <typename Pred> EntryT *insert(EntryT *E, Pred Matches)`: returns `E`, or the existing equal entry
  - `template <typename DropFn> size_t removeIf(DropFn Drop)`: returns the number removed
  - `template <typename Fn> void forEach(Fn F) const`: takes the writer lock
  - `size_t size() const`
- Entries are owned by the caller and must outlive the table.

- [ ] **Step 1: Write the failing tests** `ConcurrentTableTest.cpp`

```cpp
#include "ConcurrentTable.h"
#include "gtest/gtest.h"
#include <atomic>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

using fbsd::ConcurrentTable;

namespace {
struct E {
  uint64_t Hash;
  uint64_t Key;
};

uint64_t mix(uint64_t K) {
  K ^= K >> 33;
  K *= 0xff51afd7ed558ccdULL;
  K ^= K >> 33;
  return K;
}

struct Pool {
  std::mutex M;
  std::deque<E> Items;
  E *make(uint64_t Key, uint64_t Hash) {
    std::lock_guard<std::mutex> L(M);
    Items.push_back({Hash, Key});
    return &Items.back();
  }
  E *make(uint64_t Key) { return make(Key, mix(Key)); }
};

auto keyIs(uint64_t K) {
  return [K](const E &X) { return X.Key == K; };
}
} // namespace

TEST(FbsdConcurrentTable, FindMissingIsNull) {
  ConcurrentTable<E> T;
  EXPECT_EQ(T.find(mix(1), keyIs(1)), nullptr);
}

TEST(FbsdConcurrentTable, InsertThenFind) {
  Pool P;
  ConcurrentTable<E> T;
  E *A = P.make(7);
  EXPECT_EQ(T.insert(A, keyIs(7)), A);
  EXPECT_EQ(T.find(mix(7), keyIs(7)), A);
  EXPECT_EQ(T.size(), 1u);
}

TEST(FbsdConcurrentTable, DuplicateInsertReturnsExisting) {
  Pool P;
  ConcurrentTable<E> T;
  E *A = P.make(7);
  E *B = P.make(7);
  T.insert(A, keyIs(7));
  EXPECT_EQ(T.insert(B, keyIs(7)), A);
  EXPECT_EQ(T.size(), 1u);
}

TEST(FbsdConcurrentTable, ForcedCollisionKeepsKeysApart) {
  Pool P;
  ConcurrentTable<E> T;
  E *A = P.make(1, 42);
  E *B = P.make(2, 42);
  T.insert(A, keyIs(1));
  T.insert(B, keyIs(2));
  EXPECT_EQ(T.find(42, keyIs(1)), A);
  EXPECT_EQ(T.find(42, keyIs(2)), B);
  EXPECT_EQ(T.find(42, keyIs(3)), nullptr);
}

TEST(FbsdConcurrentTable, GrowthKeepsEntries) {
  Pool P;
  ConcurrentTable<E> T(4);
  for (uint64_t K = 0; K != 20000; ++K)
    T.insert(P.make(K), keyIs(K));
  for (uint64_t K = 0; K != 20000; ++K)
    ASSERT_NE(T.find(mix(K), keyIs(K)), nullptr) << K;
  EXPECT_EQ(T.size(), 20000u);
}

TEST(FbsdConcurrentTable, RemoveIf) {
  Pool P;
  ConcurrentTable<E> T;
  for (uint64_t K = 0; K != 100; ++K)
    T.insert(P.make(K), keyIs(K));
  EXPECT_EQ(T.removeIf([](const E &X) { return X.Key % 2 == 0; }), 50u);
  EXPECT_EQ(T.size(), 50u);
  EXPECT_EQ(T.find(mix(4), keyIs(4)), nullptr);
  EXPECT_NE(T.find(mix(5), keyIs(5)), nullptr);
}

TEST(FbsdConcurrentTable, ConcurrentInsertsAgreeOnOneEntry) {
  Pool P;
  ConcurrentTable<E> T(16);
  constexpr int Threads = 16;
  constexpr uint64_t Keys = 5000;
  std::vector<std::vector<E *>> Got(Threads, std::vector<E *>(Keys));
  std::vector<std::thread> Ts;
  for (int I = 0; I != Threads; ++I)
    Ts.emplace_back([&, I] {
      for (uint64_t K = 0; K != Keys; ++K) {
        E *Found = T.find(mix(K), keyIs(K));
        Got[I][K] = Found ? Found : T.insert(P.make(K), keyIs(K));
      }
    });
  for (auto &Th : Ts)
    Th.join();
  EXPECT_EQ(T.size(), Keys);
  for (uint64_t K = 0; K != Keys; ++K)
    for (int I = 1; I != Threads; ++I)
      ASSERT_EQ(Got[I][K], Got[0][K]) << K;
}

TEST(FbsdConcurrentTable, ReadersSeeOldEntriesDuringGrowth) {
  Pool P;
  ConcurrentTable<E> T(4);
  for (uint64_t K = 0; K != 64; ++K)
    T.insert(P.make(K), keyIs(K));
  std::atomic<bool> Stop{false};
  std::atomic<uint64_t> Misses{0};
  std::vector<std::thread> Readers;
  for (int I = 0; I != 8; ++I)
    Readers.emplace_back([&] {
      while (!Stop.load(std::memory_order_relaxed))
        for (uint64_t K = 0; K != 64; ++K)
          if (!T.find(mix(K), keyIs(K)))
            Misses.fetch_add(1);
    });
  for (uint64_t K = 64; K != 200000; ++K)
    T.insert(P.make(K), keyIs(K));
  Stop = true;
  for (auto &R : Readers)
    R.join();
  EXPECT_EQ(Misses.load(), 0u);
}
```

Add `ConcurrentTableTest.cpp` to `add_distinct_clang_unittest` in the unittest CMakeLists.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cmd //c 'D:\scratch\fbsd\build.bat FastbuildScanDepsTests' 2>&1 | tail -5
```

Expected: compile error, `ConcurrentTable.h` not found.

- [ ] **Step 3: Implement `ConcurrentTable.h`**

```cpp
#ifndef FBSD_CONCURRENTTABLE_H
#define FBSD_CONCURRENTTABLE_H

#include "llvm/Support/MathExtras.h"
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

namespace fbsd {

template <typename EntryT> class ConcurrentTable {
  struct Table {
    explicit Table(size_t Capacity)
        : Mask(Capacity - 1), Slots(new std::atomic<EntryT *>[Capacity]) {
      for (size_t I = 0; I != Capacity; ++I)
        Slots[I].store(nullptr, std::memory_order_relaxed);
    }
    size_t Mask;
    std::unique_ptr<std::atomic<EntryT *>[]> Slots;
  };

public:
  explicit ConcurrentTable(size_t ExpectedEntries = 1024) {
    publish(std::make_unique<Table>(
        llvm::PowerOf2Ceil(std::max<size_t>(ExpectedEntries * 2, 16))));
  }

  template <typename Pred> EntryT *find(uint64_t Hash, Pred Matches) const {
    return findIn(*Current.load(std::memory_order_acquire), Hash, Matches);
  }

  template <typename Pred> EntryT *insert(EntryT *E, Pred Matches) {
    std::lock_guard<std::mutex> L(WriteLock);
    Table *T = Current.load(std::memory_order_relaxed);
    if (EntryT *Existing = findIn(*T, E->Hash, Matches))
      return Existing;
    auto KeepAll = [](const EntryT &) { return false; };
    if ((Count + 1) * 2 > T->Mask + 1)
      T = rebuild(*T, (T->Mask + 1) * 2, KeepAll);
    place(*T, E);
    ++Count;
    return E;
  }

  // Readers that already loaded the old table may still find removed entries.
  template <typename DropFn> size_t removeIf(DropFn Drop) {
    std::lock_guard<std::mutex> L(WriteLock);
    Table *T = Current.load(std::memory_order_relaxed);
    size_t Before = Count;
    rebuild(*T, T->Mask + 1, Drop);
    return Before - Count;
  }

  template <typename Fn> void forEach(Fn F) const {
    std::lock_guard<std::mutex> L(WriteLock);
    const Table &T = *Current.load(std::memory_order_relaxed);
    for (size_t I = 0; I <= T.Mask; ++I)
      if (EntryT *E = T.Slots[I].load(std::memory_order_relaxed))
        F(*E);
  }

  size_t size() const {
    std::lock_guard<std::mutex> L(WriteLock);
    return Count;
  }

private:
  template <typename Pred>
  static EntryT *findIn(const Table &T, uint64_t Hash, Pred &Matches) {
    for (size_t I = Hash & T.Mask;; I = (I + 1) & T.Mask) {
      EntryT *E = T.Slots[I].load(std::memory_order_acquire);
      if (!E)
        return nullptr;
      if (E->Hash == Hash && Matches(*E))
        return E;
    }
  }

  static void place(Table &T, EntryT *E) {
    size_t I = E->Hash & T.Mask;
    while (T.Slots[I].load(std::memory_order_relaxed))
      I = (I + 1) & T.Mask;
    T.Slots[I].store(E, std::memory_order_release);
  }

  template <typename DropFn>
  Table *rebuild(const Table &Old, size_t Capacity, DropFn &Drop) {
    auto New = std::make_unique<Table>(Capacity);
    Count = 0;
    for (size_t I = 0; I <= Old.Mask; ++I)
      if (EntryT *E = Old.Slots[I].load(std::memory_order_relaxed))
        if (!Drop(*E)) {
          place(*New, E);
          ++Count;
        }
    return publish(std::move(New));
  }

  // Old tables stay alive until destruction so lock-free readers never dangle.
  Table *publish(std::unique_ptr<Table> T) {
    Table *Raw = T.get();
    Tables.push_back(std::move(T));
    Current.store(Raw, std::memory_order_release);
    return Raw;
  }

  std::atomic<Table *> Current{nullptr};
  mutable std::mutex WriteLock;
  std::vector<std::unique_ptr<Table>> Tables;
  size_t Count = 0;
};

} // namespace fbsd

#endif
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cmd //c 'D:\scratch\fbsd\build.bat FastbuildScanDepsTests' 2>&1 | tail -3 && /e/code/llvm-project-build/fbsd/tools/clang/unittests/FastbuildScanDeps/FastbuildScanDepsTests.exe --gtest_filter='FbsdConcurrentTable*' --gtest_repeat=20
```

Expected: all 8 tests PASS on all 20 repeats.

- [ ] **Step 5: Commit**

```bash
git add clang/tools/fastbuild-scan-deps/ConcurrentTable.h clang/unittests/FastbuildScanDeps
git commit -m "[fbsd] Add lock-free-read ConcurrentTable

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: CommandAdapter

**Files:**
- Create: `clang/tools/fastbuild-scan-deps/CommandAdapter.h`, `CommandAdapter.cpp`
- Create: `clang/unittests/FastbuildScanDeps/CommandAdapterTest.cpp`
- Modify: `clang/tools/fastbuild-scan-deps/CMakeLists.txt` (replace `CoreStub.cpp` with `CommandAdapter.cpp`; delete `CoreStub.cpp`), unittest CMakeLists (add the test)

**Interfaces:**
- Consumes: `FBSD_E_ARGS`, `FBSD_E_DRIVER` from `fbsd.h`.
- Produces (namespace `fbsd`):
  - `enum class DriverKind { ClangCL, Clang, CL };`
  - `std::optional<DriverKind> detectDriver(llvm::StringRef Argv0);`
  - `class AdaptError : public llvm::ErrorInfo<AdaptError>` with public `int Code; std::string Msg;`
  - `llvm::Error expandResponseFiles(llvm::StringRef Cwd, DriverKind Kind, llvm::SmallVectorImpl<const char *> &Args, llvm::BumpPtrAllocator &Alloc);`
  - `struct AdapterEnv { unsigned ClangMajor; std::string SelfClangCL; std::function<std::optional<std::string>(llvm::StringRef)> GetClVersion, FindProgram; std::function<bool(llvm::StringRef)> IsDirectory; };`
  - `AdapterEnv makeDefaultAdapterEnv(llvm::StringRef SelfClangCL);`: real Windows implementations, each cached behind a mutex
  - `struct AdaptedCommand { std::vector<std::string> Args; bool Inexact = false; };`
  - `llvm::Expected<AdaptedCommand> adaptCommand(DriverKind Kind, llvm::ArrayRef<const char *> Args, const AdapterEnv &Env);`
  - `void appendMemoKey(llvm::StringRef Cwd, llvm::StringRef IncludeEnv, llvm::StringRef ExternalIncludeEnv, llvm::ArrayRef<const char *> Args, std::string &Out);`

- [ ] **Step 1: Write the failing tests** `CommandAdapterTest.cpp`

```cpp
#include "CommandAdapter.h"
#include "fbsd.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Program.h"
#include "llvm/Support/Regex.h"
#include "llvm/Support/raw_ostream.h"
#include "gtest/gtest.h"
#include <cstdlib>

using namespace fbsd;
using namespace llvm;

namespace {
const char *ClangCL = "C:\\llvm\\bin\\clang-cl.exe";

AdapterEnv testEnv() {
  AdapterEnv Env;
  Env.ClangMajor = 23;
  Env.SelfClangCL = "C:\\fbsd\\clang-cl.exe";
  Env.GetClVersion = [](StringRef) { return std::optional<std::string>("19.51.36231"); };
  Env.FindProgram = [](StringRef Name) -> std::optional<std::string> {
    if (Name == "clang-cl.exe")
      return std::string(ClangCL);
    return std::nullopt;
  };
  Env.IsDirectory = [](StringRef) { return true; };
  return Env;
}

std::vector<std::string> adapt(std::vector<const char *> Args, bool *Inexact = nullptr,
                               AdapterEnv Env = testEnv()) {
  auto Kind = detectDriver(Args[0]);
  EXPECT_TRUE(Kind);
  auto R = adaptCommand(*Kind, Args, Env);
  EXPECT_TRUE(!!R) << toString(R.takeError());
  if (Inexact)
    *Inexact = R->Inexact;
  return R->Args;
}

int errorCode(Error E) {
  int Code = -1;
  handleAllErrors(std::move(E), [&](const AdaptError &A) { Code = A.Code; });
  return Code;
}
} // namespace

TEST(FbsdAdapter, DetectsDrivers) {
  EXPECT_EQ(detectDriver("C:\\x\\clang-cl.exe"), DriverKind::ClangCL);
  EXPECT_EQ(detectDriver("C:/x/CLANG-CL"), DriverKind::ClangCL);
  EXPECT_EQ(detectDriver("clang++"), DriverKind::Clang);
  EXPECT_EQ(detectDriver("clang.exe"), DriverKind::Clang);
  EXPECT_EQ(detectDriver("D:\\VC\\bin\\CL.EXE"), DriverKind::CL);
  EXPECT_EQ(detectDriver("gcc"), std::nullopt);
  EXPECT_EQ(detectDriver("clang-cl-wrapper.exe"), std::nullopt);
}

TEST(FbsdAdapter, StripsDepAndPchFlags) {
  bool Inexact = true;
  auto Out = adapt({ClangCL, "/c", "/showIncludes", "-showIncludes:user", "/Yupch.h",
                    "/Fpx.pch", "-Ycpch.h", "/Y-", "/Ylsym", "/MP", "/MP8", "/FS",
                    "/FIpch.h", "/sourceDependencies", "out.json", "a.cpp"},
                   &Inexact);
  EXPECT_EQ(Out, (std::vector<std::string>{ClangCL, "/c", "/FIpch.h", "a.cpp"}));
  EXPECT_FALSE(Inexact);
}

TEST(FbsdAdapter, KeepsLookalikeFlags) {
  auto Out = adapt({ClangCL, "/fp:fast", "/Fox.obj", "/Fdx.pdb", "/FC", "a.cpp"});
  EXPECT_EQ(Out, (std::vector<std::string>{ClangCL, "/fp:fast", "/Fox.obj", "/Fdx.pdb", "/FC", "a.cpp"}));
}

TEST(FbsdAdapter, TranslatesCl) {
  bool Inexact = false;
  auto Out = adapt({"D:\\VC\\cl.exe", "/c", "/Iinc", "a.cpp"}, &Inexact);
  EXPECT_EQ(Out, (std::vector<std::string>{
                     "C:\\fbsd\\clang-cl.exe", "-fms-compatibility-version=19.51.36231",
                     "-nobuiltininc", "-nostdlibinc", "/c", "/Iinc", "a.cpp",
                     "/external:env:INCLUDE", "/external:env:EXTERNAL_INCLUDE"}));
  EXPECT_TRUE(Inexact);
}

TEST(FbsdAdapter, ClWithSlashXSkipsIncludeEnv) {
  auto Out = adapt({"D:\\VC\\cl.exe", "/X", "/c", "a.cpp"});
  EXPECT_EQ(Out.back(), "a.cpp");
}

TEST(FbsdAdapter, UnreadableClVersionIsDriverError) {
  AdapterEnv Env = testEnv();
  Env.GetClVersion = [](StringRef) { return std::optional<std::string>(); };
  auto R = adaptCommand(DriverKind::CL, {"D:\\VC\\cl.exe", "a.cpp"}, Env);
  ASSERT_FALSE(!!R);
  EXPECT_EQ(errorCode(R.takeError()), FBSD_E_DRIVER);
}

TEST(FbsdAdapter, RelativeClangClResolvedOnPath) {
  auto Out = adapt({"clang-cl.exe", "/c", "a.cpp"});
  EXPECT_EQ(Out[0], ClangCL);
}

TEST(FbsdAdapter, UnresolvableRelativeDriverIsDriverError) {
  auto R = adaptCommand(DriverKind::Clang, {"clang", "a.c"}, testEnv());
  ASSERT_FALSE(!!R);
  EXPECT_EQ(errorCode(R.takeError()), FBSD_E_DRIVER);
}

TEST(FbsdAdapter, ClangMajorMismatchIsInexact) {
  AdapterEnv Env = testEnv();
  std::string Seen;
  Env.IsDirectory = [&](StringRef D) { Seen = D.str(); return false; };
  bool Inexact = false;
  adapt({ClangCL, "a.cpp"}, &Inexact, Env);
  EXPECT_TRUE(Inexact);
  EXPECT_EQ(Seen, "C:\\llvm\\lib\\clang\\23");
}

TEST(FbsdAdapter, GccStyleStripsDepFlags) {
  auto Out = adapt({"C:\\llvm\\bin\\clang.exe", "-MD", "-MF", "x.d", "-MTfoo", "-MMD",
                    "-c", "a.c"});
  EXPECT_EQ(Out, (std::vector<std::string>{"C:\\llvm\\bin\\clang.exe", "-c", "a.c"}));
}

TEST(FbsdAdapter, IncludePchIsArgsError) {
  auto R = adaptCommand(DriverKind::Clang,
                        {"C:\\llvm\\bin\\clang.exe", "-include-pch", "x.pch", "a.c"}, testEnv());
  ASSERT_FALSE(!!R);
  EXPECT_EQ(errorCode(R.takeError()), FBSD_E_ARGS);
}

TEST(FbsdAdapter, MemoKeyIsUnambiguous) {
  std::string A, B, C;
  appendMemoKey("C:\\w", "", "", {"x", "ab", "c"}, A);
  appendMemoKey("C:\\w", "", "", {"x", "a", "bc"}, B);
  appendMemoKey("C:\\v", "", "", {"x", "ab", "c"}, C);
  EXPECT_NE(A, B);
  EXPECT_NE(A, C);
  std::string D, E;
  appendMemoKey("C:\\w", "inc1", "", {"x"}, D);
  appendMemoKey("C:\\w", "inc2", "", {"x"}, E);
  EXPECT_NE(D, E);
}

TEST(FbsdAdapter, ResponseFileRelativeToCwd) {
  SmallString<128> Dir;
  ASSERT_FALSE(sys::fs::createUniqueDirectory("fbsd-rsp", Dir));
  SmallString<128> Rsp(Dir);
  sys::path::append(Rsp, "args.rsp");
  {
    std::error_code EC;
    raw_fd_ostream OS(Rsp, EC);
    ASSERT_FALSE(EC);
    OS << "/c a.cpp";
  }
  SmallVector<const char *, 8> Args{ClangCL, "@args.rsp"};
  BumpPtrAllocator Alloc;
  ASSERT_FALSE(expandResponseFiles(Dir, DriverKind::ClangCL, Args, Alloc));
  ASSERT_EQ(Args.size(), 3u);
  EXPECT_STREQ(Args[1], "/c");
  EXPECT_STREQ(Args[2], "a.cpp");
  sys::fs::remove_directories(Dir);
}

TEST(FbsdAdapter, MissingResponseFileIsArgsError) {
  SmallVector<const char *, 8> Args{ClangCL, "@nope.rsp"};
  BumpPtrAllocator Alloc;
  EXPECT_EQ(errorCode(expandResponseFiles("C:\\", DriverKind::ClangCL, Args, Alloc)),
            FBSD_E_ARGS);
}

TEST(FbsdAdapter, RealClVersion) {
  const char *Env = std::getenv("FBSD_TEST_CL");
  std::string Cl = Env ? Env : "";
  if (Cl.empty())
    if (auto P = sys::findProgramByName("cl"))
      Cl = *P;
  if (Cl.empty())
    GTEST_SKIP() << "cl.exe not found";
  auto V = makeDefaultAdapterEnv("x").GetClVersion(Cl);
  ASSERT_TRUE(V);
  EXPECT_TRUE(Regex("^19\\.[0-9]+\\.[0-9]+$").match(*V)) << *V;
}
```

If `expandResponseFiles` on a missing file does not return an error, check `clang::driver::expandResponseFiles`. LLVM's `ExpandResponseFiles` treats an unreadable `@file` as an error when `setMarkEOLs`/`setRelativeNames` are default; if it doesn't here, add an explicit existence check in `fbsd::expandResponseFiles` for each `@` argument.

- [ ] **Step 2: Run to verify failure**

```bash
cmd //c 'D:\scratch\fbsd\build.bat FastbuildScanDepsTests' 2>&1 | tail -5
```

Expected: compile error, `CommandAdapter.h` not found.

- [ ] **Step 3: Write `CommandAdapter.h`**

```cpp
#ifndef FBSD_COMMANDADAPTER_H
#define FBSD_COMMANDADAPTER_H

#include "llvm/ADT/ArrayRef.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/Support/Allocator.h"
#include "llvm/Support/Error.h"
#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace fbsd {

enum class DriverKind { ClangCL, Clang, CL };

std::optional<DriverKind> detectDriver(llvm::StringRef Argv0);

class AdaptError : public llvm::ErrorInfo<AdaptError> {
public:
  static char ID;
  AdaptError(int Code, std::string Msg) : Code(Code), Msg(std::move(Msg)) {}
  void log(llvm::raw_ostream &OS) const override;
  std::error_code convertToErrorCode() const override {
    return llvm::inconvertibleErrorCode();
  }
  int Code;
  std::string Msg;
};

llvm::Error expandResponseFiles(llvm::StringRef Cwd, DriverKind Kind,
                                llvm::SmallVectorImpl<const char *> &Args,
                                llvm::BumpPtrAllocator &Alloc);

struct AdapterEnv {
  unsigned ClangMajor = 0;
  std::string SelfClangCL;
  std::function<std::optional<std::string>(llvm::StringRef ClExe)> GetClVersion;
  std::function<std::optional<std::string>(llvm::StringRef Program)> FindProgram;
  std::function<bool(llvm::StringRef Dir)> IsDirectory;
};

AdapterEnv makeDefaultAdapterEnv(llvm::StringRef SelfClangCL);

struct AdaptedCommand {
  std::vector<std::string> Args;
  bool Inexact = false;
};

llvm::Expected<AdaptedCommand> adaptCommand(DriverKind Kind,
                                            llvm::ArrayRef<const char *> Args,
                                            const AdapterEnv &Env);

void appendMemoKey(llvm::StringRef Cwd, llvm::StringRef IncludeEnv,
                   llvm::StringRef ExternalIncludeEnv,
                   llvm::ArrayRef<const char *> Args, std::string &Out);

} // namespace fbsd

#endif
```

- [ ] **Step 4: Write `CommandAdapter.cpp`**

```cpp
#include "CommandAdapter.h"
#include "fbsd.h"
#include "clang/Basic/Version.h"
#include "clang/Driver/Driver.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/FormatVariadic.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Program.h"
#include "llvm/Support/VirtualFileSystem.h"
#include "llvm/Support/Windows/WindowsSupport.h"
#include <mutex>

using namespace llvm;

namespace fbsd {

char AdaptError::ID;

void AdaptError::log(raw_ostream &OS) const { OS << Msg; }

static Error adaptError(int Code, const Twine &Msg) {
  return make_error<AdaptError>(Code, Msg.str());
}

std::optional<DriverKind> detectDriver(StringRef Argv0) {
  StringRef Name = sys::path::filename(Argv0);
  if (Name.ends_with_insensitive(".exe"))
    Name = Name.drop_back(4);
  if (Name.equals_insensitive("clang-cl"))
    return DriverKind::ClangCL;
  if (Name.equals_insensitive("clang") || Name.equals_insensitive("clang++"))
    return DriverKind::Clang;
  if (Name.equals_insensitive("cl"))
    return DriverKind::CL;
  return std::nullopt;
}

Error expandResponseFiles(StringRef Cwd, DriverKind Kind,
                          SmallVectorImpl<const char *> &Args,
                          BumpPtrAllocator &Alloc) {
  if (none_of(Args, [](const char *A) { return A[0] == '@'; }))
    return Error::success();
  std::unique_ptr<vfs::FileSystem> FS = vfs::createPhysicalFileSystem();
  if (std::error_code EC = FS->setCurrentWorkingDirectory(Cwd))
    return adaptError(FBSD_E_ARGS, "fbsd: bad cwd '" + Cwd + "': " + EC.message());
  for (const char *A : Args)
    if (A[0] == '@' && !FS->exists(A + 1))
      return adaptError(FBSD_E_ARGS, Twine("fbsd: response file not found: ") + (A + 1));
  if (Error E = clang::driver::expandResponseFiles(Args, Kind != DriverKind::Clang,
                                                  Alloc, FS.get()))
    return adaptError(FBSD_E_ARGS, "fbsd: " + toString(std::move(E)));
  return Error::success();
}

static bool isClFlag(StringRef A, StringRef Name) {
  return A.size() > 1 && (A[0] == '/' || A[0] == '-') && A.substr(1) == Name;
}

static bool hasClPrefix(StringRef A, StringRef Prefix) {
  return A.size() > 1 && (A[0] == '/' || A[0] == '-') &&
         A.substr(1).starts_with(Prefix);
}

static unsigned clStripCount(ArrayRef<const char *> Args, size_t I) {
  StringRef A = Args[I];
  if (isClFlag(A, "showIncludes") || isClFlag(A, "showIncludes:user") ||
      isClFlag(A, "FS") || isClFlag(A, "Y-"))
    return 1;
  if (hasClPrefix(A, "Yc") || hasClPrefix(A, "Yu") || hasClPrefix(A, "Fp") ||
      hasClPrefix(A, "Yl"))
    return 1;
  if (hasClPrefix(A, "MP") && all_of(A.substr(3), isDigit))
    return 1;
  if (isClFlag(A, "sourceDependencies") ||
      isClFlag(A, "sourceDependencies:directives"))
    return I + 1 < Args.size() ? 2 : 1;
  return 0;
}

static unsigned gccStripCount(ArrayRef<const char *> Args, size_t I) {
  StringRef A = Args[I];
  if (A == "-M" || A == "-MM" || A == "-MD" || A == "-MMD" || A == "-MG" ||
      A == "-MP")
    return 1;
  if (A == "-MF" || A == "-MT" || A == "-MQ" || A == "-MJ")
    return I + 1 < Args.size() ? 2 : 1;
  if (A.starts_with("-MF") || A.starts_with("-MT") || A.starts_with("-MQ") ||
      A.starts_with("-MJ"))
    return 1;
  return 0;
}

Expected<AdaptedCommand> adaptCommand(DriverKind Kind, ArrayRef<const char *> Args,
                                      const AdapterEnv &Env) {
  AdaptedCommand Out;
  Out.Args.reserve(Args.size() + 6);
  const bool ClMode = Kind != DriverKind::Clang;
  StringRef Argv0 = Args[0];

  if (Kind == DriverKind::CL) {
    std::optional<std::string> Ver = Env.GetClVersion(Argv0);
    if (!Ver)
      return adaptError(FBSD_E_DRIVER, "fbsd: cannot read version of '" + Argv0 + "'");
    Out.Args.push_back(Env.SelfClangCL);
    Out.Args.push_back("-fms-compatibility-version=" + *Ver);
    Out.Args.push_back("-nobuiltininc");
    Out.Args.push_back("-nostdlibinc");
    Out.Inexact = true;
  } else {
    std::string Exe = Argv0.str();
    if (!sys::path::is_absolute(Exe)) {
      std::optional<std::string> Found = Env.FindProgram(Exe);
      if (!Found)
        return adaptError(FBSD_E_DRIVER, "fbsd: '" + Argv0 + "' not found on PATH");
      Exe = std::move(*Found);
    }
    SmallString<256> Res(sys::path::parent_path(sys::path::parent_path(Exe)));
    sys::path::append(Res, "lib", "clang", Twine(Env.ClangMajor));
    if (!Env.IsDirectory(Res))
      Out.Inexact = true;
    Out.Args.push_back(std::move(Exe));
  }

  bool NoIncludeEnv = false;
  for (size_t I = 1; I < Args.size();) {
    StringRef A = Args[I];
    if (!ClMode && A == "-include-pch")
      return adaptError(FBSD_E_ARGS, "fbsd: -include-pch is not supported");
    unsigned Skip = ClMode ? clStripCount(Args, I) : gccStripCount(Args, I);
    if (Skip) {
      I += Skip;
      continue;
    }
    if (ClMode && isClFlag(A, "X"))
      NoIncludeEnv = true;
    Out.Args.push_back(A.str());
    ++I;
  }

  if (Kind == DriverKind::CL && !NoIncludeEnv) {
    Out.Args.push_back("/external:env:INCLUDE");
    Out.Args.push_back("/external:env:EXTERNAL_INCLUDE");
  }
  return Out;
}

void appendMemoKey(StringRef Cwd, StringRef IncludeEnv, StringRef ExternalIncludeEnv,
                   ArrayRef<const char *> Args, std::string &Out) {
  auto Put = [&Out](StringRef S) {
    uint32_t N = static_cast<uint32_t>(S.size());
    Out.append(reinterpret_cast<const char *>(&N), sizeof(N));
    Out.append(S.data(), S.size());
  };
  Put(Cwd);
  Put(IncludeEnv);
  Put(ExternalIncludeEnv);
  for (const char *A : Args)
    Put(A);
}

static std::optional<std::string> readClVersion(StringRef Exe) {
  SmallVector<wchar_t, 260> W;
  if (sys::windows::UTF8ToUTF16(Exe, W))
    return std::nullopt;
  DWORD Unused = 0;
  DWORD Size = GetFileVersionInfoSizeW(W.data(), &Unused);
  if (!Size)
    return std::nullopt;
  std::vector<char> Buf(Size);
  if (!GetFileVersionInfoW(W.data(), 0, Size, Buf.data()))
    return std::nullopt;
  VS_FIXEDFILEINFO *Info = nullptr;
  UINT Len = 0;
  if (!VerQueryValueW(Buf.data(), L"\\", reinterpret_cast<void **>(&Info), &Len) ||
      Len < sizeof(*Info))
    return std::nullopt;
  return formatv("{0}.{1}.{2}", HIWORD(Info->dwFileVersionMS),
                 LOWORD(Info->dwFileVersionMS), HIWORD(Info->dwFileVersionLS))
      .str();
}

template <typename T> class Memoized {
public:
  explicit Memoized(std::function<T(StringRef)> F) : F(std::move(F)) {}
  T operator()(StringRef Key) {
    std::lock_guard<std::mutex> L(M);
    auto [It, Inserted] = Cache.try_emplace(Key);
    if (Inserted)
      It->second = F(Key);
    return It->second;
  }

private:
  std::function<T(StringRef)> F;
  std::mutex M;
  StringMap<T> Cache;
};

AdapterEnv makeDefaultAdapterEnv(StringRef SelfClangCL) {
  AdapterEnv Env;
  Env.ClangMajor = CLANG_VERSION_MAJOR;
  Env.SelfClangCL = SelfClangCL.str();
  auto Ver = std::make_shared<Memoized<std::optional<std::string>>>(readClVersion);
  Env.GetClVersion = [Ver](StringRef P) { return (*Ver)(P); };
  auto Find = std::make_shared<Memoized<std::optional<std::string>>>(
      [](StringRef Name) -> std::optional<std::string> {
        if (ErrorOr<std::string> P = sys::findProgramByName(Name))
          return *P;
        return std::nullopt;
      });
  Env.FindProgram = [Find](StringRef P) { return (*Find)(P); };
  auto IsDir = std::make_shared<Memoized<bool>>(
      [](StringRef D) { return sys::fs::is_directory(D); });
  Env.IsDirectory = [IsDir](StringRef D) { return (*IsDir)(D); };
  return Env;
}

} // namespace fbsd
```

The adapter runs only on memo misses, so the mutex in `Memoized` is off the hot path.

- [ ] **Step 5: Update CMake.** In `clang/tools/fastbuild-scan-deps/CMakeLists.txt`, replace `CoreStub.cpp` with `CommandAdapter.cpp`, then delete `CoreStub.cpp`. Add `CommandAdapterTest.cpp` to the unittest target.

- [ ] **Step 6: Run the tests**

```bash
cmd //c 'D:\scratch\fbsd\build.bat FastbuildScanDepsTests' 2>&1 | tail -3 && /e/code/llvm-project-build/fbsd/tools/clang/unittests/FastbuildScanDeps/FastbuildScanDepsTests.exe --gtest_filter='FbsdAdapter*'
```

Expected: all PASS. `RealClVersion` passes when run from the VS environment, and skips otherwise.

- [ ] **Step 7: Commit**

```bash
git add -A clang/tools/fastbuild-scan-deps clang/unittests/FastbuildScanDeps
git commit -m "[fbsd] Add CommandAdapter: driver detection, flag stripping, cl translation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Scan path — Context, InternTable, InternConsumer, C API, `fbsd-cli run`

No memo yet: every call scans.

**Files:**
- Create: `clang/tools/fastbuild-scan-deps/InternTable.h`, `InternTable.cpp`, `InternConsumer.h`, `InternConsumer.cpp`, `Context.h`, `Context.cpp`
- Modify: `fbsd.cpp`, `fbsd.exports`, `CMakeLists.txt` (add the sources), `cli/FbsdCli.cpp` (full `Api`, `run` command)
- Create lit tests: `clang/test/FastbuildScanDeps/include-forms.c`, `dedup-spellings.c`, `junction.c`, `unicode.c`, `errors.c`, `error-then-ok.c`, `cl-translate.c`

**Interfaces:**
- Consumes: `ConcurrentTable`, `detectDriver`, `expandResponseFiles`, `adaptCommand`, `makeDefaultAdapterEnv`, `appendMemoKey` (Task 5 uses it).
- Produces:
  - `struct fbsd::InternEntry { uint64_t Hash; llvm::sys::fs::UniqueID UID; uint32_t Index; const char *Path; };`
  - `class fbsd::InternTable` with `find(UniqueID) const`, `insert(UniqueID, StringRef RealPath, BumpPtrAllocator &)`, `indexLimit()`, `forEach(Fn)`, `size()`
  - `struct fbsd::ThreadState` (Context.h): `Arena`, `Worker`, `WorkerGen`, `Seen`, `SeenStamp`, `Files`, `KeyBuf`, `Diag`, `bool markSeen(uint32_t Index)`, `void beginScan(uint32_t IndexLimit)`
  - `class fbsd::InternConsumer : clang::dependencies::DependencyConsumer`
  - `class fbsd::Context` with `int scan(StringRef Cwd, ArrayRef<const char *> Argv, fbsd_result &Out, const char *&Diag)`, `uint32_t invalidate(ArrayRef<const char *>)` (returns 0 until Task 6), `void stats(fbsd_stats_out &) const`
  - `fbsd-cli run SCRIPT [--define K=V]... [--relative-to DIR]`. The script is JSON lines: `{"op":"scan","cwd":S,"argv":[S...]}`, `{"op":"invalidate","paths":[S...]}`, `{"op":"stats"}`, `{"op":"scan-parallel","threads":N,"cwd":S,"argv":[...]}`. `${NAME}` in any string is replaced by a define; `${BIN}` defaults to fbsd-cli's directory.
  - Output formats:
    - scan: `scan rc=<int> flags=<exact|inexact>[,memo] count=<n>`, then `  <path>` per file, then `  diag: <line>` per diag line on error
    - invalidate: `invalidate dropped=<n>`
    - stats: `stats scans=<n> memo_hits=<n> invalidated=<n> service_resets=<n>`
    - scan-parallel: `scan-parallel rc=<int> distinct_results=<n> count=<n>`
  - Paths under `--relative-to DIR` (case-insensitive prefix) print as `T/<rest>`, with `/` separators and the rest's case kept.

- [ ] **Step 1: Write the lit tests (they fail until the CLI and DLL exist)**

`clang/test/FastbuildScanDeps/include-forms.c`:

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK:      scan rc=0 flags=exact count=7
// CHECK-NEXT:   T/src/main.cpp
// CHECK-NEXT:   T/inc/forced.h
// CHECK-DAG:    T/inc/quoted.h
// CHECK-DAG:    T/inc/angled.h
// CHECK-DAG:    T/inc/next.h
// CHECK-DAG:    T/inc2/next.h
// CHECK-DAG:    T/inc/probe.h
// CHECK-NOT:  T/

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","/FIforced.h","/Iinc","/Iinc2","src/main.cpp"]}
//--- src/main.cpp
#include "quoted.h"
#include <angled.h>
#include <next.h>
#if __has_include(<probe.h>)
#endif
#if __has_include(<absent.h>)
#include <absent.h>
#endif
//--- inc/forced.h
//--- inc/quoted.h
#pragma once
#include "quoted.h"
//--- inc/angled.h
//--- inc/next.h
#include_next <next.h>
//--- inc2/next.h
```

`dedup-spellings.c`:

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK:      scan rc=0 flags=exact count=2
// CHECK-NEXT:   T/main.cpp
// CHECK-NEXT:   T/sub/Foo.h

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","/Isub","/Isub/../sub","main.cpp"]}
//--- main.cpp
#include "sub/Foo.h"
#include "SUB/foo.h"
#include "sub/../sub/Foo.h"
#include <foo.h>
//--- sub/Foo.h
#pragma once
```

`junction.c`:

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: %python -c "import _winapi,sys; _winapi.CreateJunction(sys.argv[1], sys.argv[2])" %t/real %t/junc
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK:      scan rc=0 flags=exact count=2
// CHECK-NEXT:   T/main.cpp
// CHECK-NEXT:   T/real/h.h

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","main.cpp"]}
//--- main.cpp
#include "junc/h.h"
#include "real/h.h"
//--- real/h.h
#pragma once
```

`unicode.c` (Review Focus 3):

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t/dïr-ü --relative-to %t | FileCheck %s

// CHECK:      scan rc=0 flags=exact count=2
// CHECK-NEXT:   T/dïr-ü/main.cpp
// CHECK-NEXT:   T/dïr-ü/hé.h

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","main.cpp"]}
//--- dïr-ü/main.cpp
#include "hé.h"
//--- dïr-ü/hé.h
```

`errors.c`:

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK:      scan rc=2 flags=exact count=0
// CHECK-NEXT:   diag: fbsd: unknown driver 'gcc.exe'
// CHECK:      scan rc=3 flags=exact count=0
// CHECK:        diag: {{.*}}'missing.h' file not found
// CHECK:      scan rc=1 flags=exact count=0
// CHECK-NEXT:   diag: fbsd: response file not found: nope.rsp

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["gcc.exe","-c","main.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","main.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","@nope.rsp"]}
//--- main.cpp
#include "missing.h"
```

`error-then-ok.c` (Review Focus 5):

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK:      scan rc=3
// CHECK:      scan rc=0 flags=exact count=2
// CHECK-NEXT:   T/good.cpp
// CHECK-NEXT:   T/ok.h
// CHECK-NOT:    diag:

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","bad.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","good.cpp"]}
//--- bad.cpp
#include "ok.h"
#include "missing.h"
//--- good.cpp
#include "ok.h"
//--- ok.h
#pragma once
```

`cl-translate.c`: a real cl command line, scanned inexact. `_MSC_VER` must match cl's.

```c
// REQUIRES: fbsd-cl
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --define CL=%{cl} --relative-to %t | FileCheck %s

// CHECK:      scan rc=0 flags=inexact count=2
// CHECK-NEXT:   T/main.cpp
// CHECK-NEXT:   T/ver19.h

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${CL}","/c","/Yupch.h","/Fpx.pch","main.cpp"]}
//--- main.cpp
#if _MSC_VER >= 1900 && _MSC_VER < 2000
#include "ver19.h"
#else
#include "wrong.h"
#endif
//--- ver19.h
```

- [ ] **Step 2: Run the lit tests to verify they fail**

```bash
cmd //c 'D:\scratch\fbsd\check.bat E:\code\llvm-project\clang\test\FastbuildScanDeps' 2>&1 | tail -15
```

Expected: `version.c` PASS; the new tests FAIL with `fbsd-cli: unknown command run`.

- [ ] **Step 3: Write `InternTable.h` and `InternTable.cpp`**

```cpp
#ifndef FBSD_INTERNTABLE_H
#define FBSD_INTERNTABLE_H

#include "ConcurrentTable.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/Support/Allocator.h"
#include "llvm/Support/FileSystem/UniqueID.h"
#include <atomic>

namespace fbsd {

struct InternEntry {
  uint64_t Hash;
  llvm::sys::fs::UniqueID UID;
  uint32_t Index;
  const char *Path;
};

class InternTable {
public:
  explicit InternTable(size_t ExpectedFiles) : Table(ExpectedFiles) {}

  const InternEntry *find(llvm::sys::fs::UniqueID UID) const {
    return Table.find(hashUID(UID), [&](const InternEntry &E) { return E.UID == UID; });
  }

  const InternEntry *insert(llvm::sys::fs::UniqueID UID, llvm::StringRef RealPath,
                            llvm::BumpPtrAllocator &Arena);

  uint32_t indexLimit() const { return NextIndex.load(std::memory_order_acquire); }
  size_t size() const { return Table.size(); }
  template <typename Fn> void forEach(Fn F) const { Table.forEach(F); }

private:
  static uint64_t hashUID(llvm::sys::fs::UniqueID UID);

  ConcurrentTable<InternEntry> Table;
  std::atomic<uint32_t> NextIndex{0};
};

} // namespace fbsd

#endif
```

```cpp
#include "InternTable.h"
#include "llvm/Support/xxhash.h"
#include <cstring>

using namespace llvm;

namespace fbsd {

uint64_t InternTable::hashUID(sys::fs::UniqueID UID) {
  uint64_t Words[2] = {UID.getDevice(), UID.getFile()};
  return xxh3_64bits(reinterpret_cast<const uint8_t *>(Words), sizeof(Words));
}

const InternEntry *InternTable::insert(sys::fs::UniqueID UID, StringRef RealPath,
                                       BumpPtrAllocator &Arena) {
  char *P = Arena.Allocate<char>(RealPath.size() + 1);
  std::memcpy(P, RealPath.data(), RealPath.size());
  P[RealPath.size()] = '\0';
  auto *E = new (Arena.Allocate<InternEntry>())
      InternEntry{hashUID(UID), UID, NextIndex.fetch_add(1, std::memory_order_acq_rel), P};
  return Table.insert(E, [&](const InternEntry &O) { return O.UID == UID; });
}

} // namespace fbsd
```

`ConcurrentTable<InternEntry>::find` returns `InternEntry *`; the implicit conversion to `const InternEntry *` is fine.

- [ ] **Step 4: Write `Context.h`**

```cpp
#ifndef FBSD_CONTEXT_H
#define FBSD_CONTEXT_H

#include "CommandAdapter.h"
#include "InternTable.h"
#include "fbsd.h"
#include "clang/DependencyScanning/DependencyScanningService.h"
#include "clang/DependencyScanning/DependencyScanningWorker.h"
#include "llvm/Support/Allocator.h"
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace fbsd {

struct ServiceSlot {
  explicit ServiceSlot(uint64_t Gen);
  clang::dependencies::DependencyScanningService Service;
  uint64_t Gen;
};

struct ThreadState {
  llvm::BumpPtrAllocator Arena;
  std::unique_ptr<clang::dependencies::DependencyScanningWorker> Worker;
  uint64_t WorkerGen = ~0ull;
  std::vector<uint32_t> Seen;
  uint32_t SeenStamp = 0;
  std::vector<const char *> Files;
  std::string KeyBuf;
  std::string Diag;

  void beginScan(uint32_t IndexLimit);
  bool markSeen(uint32_t Index);
};

class Context {
public:
  explicit Context(const fbsd_options &Opts);
  ~Context();

  int scan(llvm::StringRef Cwd, llvm::ArrayRef<const char *> Argv, fbsd_result &Out,
           const char *&Diag);
  uint32_t invalidate(llvm::ArrayRef<const char *> Paths);
  void stats(fbsd_stats_out &Out) const;

private:
  ThreadState &threadState();
  clang::dependencies::DependencyScanningWorker &worker(ThreadState &TS);
  int fail(ThreadState &TS, int Code, const llvm::Twine &Msg, const char *&Diag);
  int scanUncached(ThreadState &TS, llvm::StringRef Cwd, const AdaptedCommand &Cmd,
                   fbsd_result &Out, const char *&Diag);

  const uint64_t Serial;
  AdapterEnv Env;
  std::string IncludeEnv, ExternalIncludeEnv;
  InternTable Interns;

  std::atomic<ServiceSlot *> CurrentService{nullptr};
  std::mutex ServicesLock;
  std::vector<std::unique_ptr<ServiceSlot>> Services;

  std::mutex ThreadsLock;
  std::vector<std::unique_ptr<ThreadState>> Threads;

  std::atomic<bool> Poisoned{false};

  std::atomic<uint64_t> Scans{0}, MemoHits{0}, InvalidateCalls{0},
      InvalidatedEntries{0}, ServiceResets{0};
  std::atomic<uint64_t> Errors[FBSD_E_COUNT] = {};
  std::atomic<uint64_t> ScanNs[FBSD_HIST_BUCKETS] = {};
  std::atomic<uint64_t> MemoHitNs[FBSD_HIST_BUCKETS] = {};
};

} // namespace fbsd

#endif
```

- [ ] **Step 5: Write `InternConsumer.h` and `InternConsumer.cpp`**

```cpp
#ifndef FBSD_INTERNCONSUMER_H
#define FBSD_INTERNCONSUMER_H

#include "Context.h"
#include "clang/DependencyScanning/DependencyConsumer.h"
#include "llvm/Support/VirtualFileSystem.h"

namespace fbsd {

class InternConsumer final : public clang::dependencies::DependencyConsumer {
public:
  InternConsumer(llvm::vfs::FileSystem &FS, InternTable &Interns, ThreadState &TS)
      : FS(FS), Interns(Interns), TS(TS) {}

  void handleFileDependency(llvm::StringRef File) override;
  void handleDependencyOutputOpts(const clang::DependencyOutputOptions &) override {}
  void handlePrebuiltModuleDependency(clang::dependencies::PrebuiltModuleDep) override {}
  void handleModuleDependency(clang::dependencies::ModuleDeps) override {}
  void handleDirectModuleDependency(clang::dependencies::ModuleID) override {}
  void handleVisibleModule(std::string) override {}
  void handleContextHash(std::string) override {}

  bool failed() const { return !Error.empty(); }
  const std::string &error() const { return Error; }

private:
  llvm::vfs::FileSystem &FS;
  InternTable &Interns;
  ThreadState &TS;
  std::string Error;
};

} // namespace fbsd

#endif
```

```cpp
#include "InternConsumer.h"
#include "llvm/ADT/SmallString.h"

using namespace llvm;

namespace fbsd {

void InternConsumer::handleFileDependency(StringRef File) {
  if (failed())
    return;
  ErrorOr<vfs::Status> St = FS.status(File);
  if (!St) {
    Error = ("fbsd: cannot stat dependency '" + File + "': " + St.getError().message()).str();
    return;
  }
  const InternEntry *E = Interns.find(St->getUniqueID());
  if (!E) {
    SmallString<256> Real;
    if (std::error_code EC = FS.getRealPath(File, Real)) {
      Error = ("fbsd: cannot resolve real path of '" + File + "': " + EC.message()).str();
      return;
    }
    E = Interns.insert(St->getUniqueID(), Real, TS.Arena);
  }
  if (TS.markSeen(E->Index))
    TS.Files.push_back(E->Path);
}

} // namespace fbsd
```

- [ ] **Step 6: Write `Context.cpp`**

```cpp
#include "Context.h"
#include "InternConsumer.h"
#include "clang/Basic/DiagnosticOptions.h"
#include "clang/DependencyScanning/DependencyScanningUtils.h"
#include "clang/Frontend/TextDiagnosticPrinter.h"
#include "clang/Tooling/DependencyScanningTool.h"
#include "llvm/ADT/bit.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Process.h"
#include "llvm/Support/Windows/WindowsSupport.h"
#include "llvm/Support/raw_ostream.h"
#include <chrono>
#include <psapi.h>

using namespace llvm;
using namespace clang::dependencies;

namespace fbsd {

namespace {
std::atomic<uint64_t> NextSerial{1};

struct TlsCache {
  uint64_t Serial = 0;
  ThreadState *TS = nullptr;
};
thread_local TlsCache Tls;

DependencyScanningServiceOptions serviceOptions() {
  DependencyScanningServiceOptions O;
  O.Mode = ScanningMode::DependencyDirectivesScan;
  O.EmitWarnings = false;
  O.ReportAbsolutePaths = true;
  O.CacheNegativeStats = true;
  return O;
}

std::string selfDir() {
  HMODULE M = nullptr;
  GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                         GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                     reinterpret_cast<LPCWSTR>(&selfDir), &M);
  wchar_t Buf[MAX_PATH * 4];
  DWORD N = GetModuleFileNameW(M, Buf, std::size(Buf));
  SmallString<260> Utf8;
  sys::windows::UTF16ToUTF8(Buf, N, Utf8);
  return std::string(sys::path::parent_path(Utf8));
}

unsigned bucket(uint64_t Ns) {
  unsigned B = Ns ? 63 - countl_zero(Ns) : 0;
  return std::min<unsigned>(B, FBSD_HIST_BUCKETS - 1);
}

void addHist(std::atomic<uint64_t> *H, std::chrono::steady_clock::time_point Start) {
  auto Ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - Start)
                .count();
  H[bucket(static_cast<uint64_t>(Ns))].fetch_add(1, std::memory_order_relaxed);
}
} // namespace

ServiceSlot::ServiceSlot(uint64_t Gen) : Service(serviceOptions()), Gen(Gen) {}

void ThreadState::beginScan(uint32_t IndexLimit) {
  Files.clear();
  if (Seen.size() < IndexLimit)
    Seen.resize(std::max<size_t>(IndexLimit, Seen.size() * 2), 0);
  if (++SeenStamp == 0) {
    std::fill(Seen.begin(), Seen.end(), 0);
    SeenStamp = 1;
  }
}

bool ThreadState::markSeen(uint32_t Index) {
  if (Index >= Seen.size())
    Seen.resize(std::max<size_t>(Index + 1, Seen.size() * 2), 0);
  if (Seen[Index] == SeenStamp)
    return false;
  Seen[Index] = SeenStamp;
  return true;
}

Context::Context(const fbsd_options &Opts)
    : Serial(NextSerial.fetch_add(1)),
      Env(makeDefaultAdapterEnv(selfDir() + "\\clang-cl.exe")),
      Interns(std::max<size_t>(Opts.expected_tus, 1024) * 8) {
  IncludeEnv = sys::Process::GetEnv("INCLUDE").value_or("");
  ExternalIncludeEnv = sys::Process::GetEnv("EXTERNAL_INCLUDE").value_or("");
  auto Slot = std::make_unique<ServiceSlot>(0);
  CurrentService.store(Slot.get(), std::memory_order_release);
  Services.push_back(std::move(Slot));
}

Context::~Context() = default;

ThreadState &Context::threadState() {
  if (Tls.Serial == Serial)
    return *Tls.TS;
  auto TS = std::make_unique<ThreadState>();
  ThreadState *Raw = TS.get();
  {
    std::lock_guard<std::mutex> L(ThreadsLock);
    Threads.push_back(std::move(TS));
  }
  Tls = {Serial, Raw};
  return *Raw;
}

DependencyScanningWorker &Context::worker(ThreadState &TS) {
  ServiceSlot *Slot = CurrentService.load(std::memory_order_acquire);
  if (!TS.Worker || TS.WorkerGen != Slot->Gen) {
    TS.Worker = std::make_unique<DependencyScanningWorker>(Slot->Service);
    TS.WorkerGen = Slot->Gen;
  }
  return *TS.Worker;
}

int Context::fail(ThreadState &TS, int Code, const Twine &Msg, const char *&Diag) {
  TS.Diag = Msg.str();
  Diag = TS.Diag.c_str();
  Errors[Code].fetch_add(1, std::memory_order_relaxed);
  return Code;
}

int Context::scan(StringRef Cwd, ArrayRef<const char *> Argv, fbsd_result &Out,
                  const char *&Diag) {
  auto Start = std::chrono::steady_clock::now();
  ThreadState &TS = threadState();
  Diag = nullptr;
  Out = {nullptr, 0, 0};
  Scans.fetch_add(1, std::memory_order_relaxed);
  if (Poisoned.load(std::memory_order_acquire))
    return fail(TS, FBSD_E_CRASH, "fbsd: context is poisoned by an earlier crash", Diag);

  std::optional<DriverKind> Kind = detectDriver(Argv[0]);
  if (!Kind)
    return fail(TS, FBSD_E_DRIVER, "fbsd: unknown driver '" + StringRef(Argv[0]) + "'", Diag);

  BumpPtrAllocator RspAlloc;
  SmallVector<const char *, 256> Args(Argv.begin(), Argv.end());
  auto FailFrom = [&](Error E) {
    int Code = FBSD_E_ARGS;
    std::string Msg;
    handleAllErrors(std::move(E), [&](const AdaptError &A) {
      Code = A.Code;
      Msg = A.Msg;
    });
    return fail(TS, Code, Msg, Diag);
  };
  if (Error E = expandResponseFiles(Cwd, *Kind, Args, RspAlloc))
    return FailFrom(std::move(E));

  Expected<AdaptedCommand> Cmd = adaptCommand(*Kind, Args, Env);
  if (!Cmd)
    return FailFrom(Cmd.takeError());

  int RC = scanUncached(TS, Cwd, *Cmd, Out, Diag);
  addHist(ScanNs, Start);
  return RC;
}

int Context::scanUncached(ThreadState &TS, StringRef Cwd, const AdaptedCommand &Cmd,
                          fbsd_result &Out, const char *&Diag) {
  DependencyScanningWorker &W = worker(TS);
  IntrusiveRefCntPtr<vfs::FileSystem> FS = W.makeEffectiveVFS(Cwd);
  TS.beginScan(Interns.indexLimit());
  InternConsumer Consumer(*FS, Interns, TS);
  CallbackActionController Controller(nullptr);
  std::string DiagText;
  raw_string_ostream DiagOS(DiagText);
  clang::DiagnosticOptions DiagOpts;
  clang::TextDiagnosticPrinter DiagPrinter(DiagOS, DiagOpts);

  bool Ok = clang::tooling::computeDependencies(W, Cwd, Cmd.Args, Consumer, Controller,
                                                DiagPrinter);
  if (!Ok)
    return fail(TS, FBSD_E_SCAN, DiagText.empty() ? "fbsd: scan failed" : DiagText, Diag);
  if (Consumer.failed())
    return fail(TS, FBSD_E_SCAN, Consumer.error(), Diag);

  const char **Files = TS.Arena.Allocate<const char *>(TS.Files.size());
  std::copy(TS.Files.begin(), TS.Files.end(), Files);
  Out = {Files, static_cast<uint32_t>(TS.Files.size()),
         Cmd.Inexact ? FBSD_RESULT_INEXACT : 0u};
  return FBSD_OK;
}

uint32_t Context::invalidate(ArrayRef<const char *>) {
  InvalidateCalls.fetch_add(1, std::memory_order_relaxed);
  return 0;
}

void Context::stats(fbsd_stats_out &Out) const {
  fbsd_stats_out S = {};
  S.struct_size = sizeof(S);
  S.scans = Scans.load();
  S.memo_hits = MemoHits.load();
  for (int I = 0; I != FBSD_E_COUNT; ++I)
    S.errors[I] = Errors[I].load();
  S.invalidate_calls = InvalidateCalls.load();
  S.invalidated_entries = InvalidatedEntries.load();
  S.service_resets = ServiceResets.load();
  S.interned_files = Interns.size();
  PROCESS_MEMORY_COUNTERS_EX PMC = {};
  if (GetProcessMemoryInfo(GetCurrentProcess(),
                           reinterpret_cast<PROCESS_MEMORY_COUNTERS *>(&PMC), sizeof(PMC)))
    S.process_private_bytes = PMC.PrivateUsage;
  for (int I = 0; I != FBSD_HIST_BUCKETS; ++I) {
    S.scan_ns[I] = ScanNs[I].load();
    S.memo_hit_ns[I] = MemoHitNs[I].load();
  }
  uint32_t N = std::min<uint32_t>(Out.struct_size, sizeof(S));
  std::memcpy(&Out, &S, N);
  Out.struct_size = N;
}

} // namespace fbsd
```

Notes for the implementer:
- `arena_bytes` is filled in Task 5, when it can sum `ThreadState::Arena.getTotalMemory()` under `ThreadsLock`. `stats` is `const`, so make `ThreadsLock` `mutable` then.
- Link `psapi` for `GetProcessMemoryInfo`: add `psapi` to `target_link_libraries(fbsdCore PRIVATE version psapi)`.
- If the `DependencyConsumer` overrides don't match (for example `handleDependencyOutputOpts` taking a `clang::` vs `clang::dependencies::` type), copy the exact signatures from `clang/include/clang/DependencyScanning/DependencyConsumer.h`.

- [ ] **Step 7: Write the C shims** `fbsd.cpp`

```cpp
#include "Context.h"
#include "fbsd.h"
#include <algorithm>
#include <cstring>
#include <new>

struct fbsd_ctx {
  fbsd::Context Impl;
  explicit fbsd_ctx(const fbsd_options &O) : Impl(O) {}
};

extern "C" uint32_t fbsd_api_version(void) { return FBSD_API_VERSION; }

extern "C" fbsd_ctx *fbsd_create(const fbsd_options *opts) {
  fbsd_options O = {sizeof(fbsd_options), 0, 0};
  if (opts)
    std::memcpy(&O, opts, std::min<size_t>(opts->struct_size, sizeof(O)));
  return new (std::nothrow) fbsd_ctx(O);
}

extern "C" void fbsd_destroy(fbsd_ctx *ctx) { delete ctx; }

extern "C" int fbsd_scan(fbsd_ctx *ctx, const char *cwd, const char *const *argv,
                         uint32_t argc, fbsd_result *out, const char **diag) {
  static const char *const BadArgs = "fbsd: null ctx/cwd/argv/out or argc == 0";
  const char *Unused = nullptr;
  const char *&D = diag ? *diag : Unused;
  if (!ctx || !cwd || !argv || !argc || !out) {
    D = BadArgs;
    return FBSD_E_ARGS;
  }
  return ctx->Impl.scan(cwd, llvm::ArrayRef<const char *>(argv, argc), *out, D);
}

extern "C" uint32_t fbsd_invalidate(fbsd_ctx *ctx, const char *const *paths, uint32_t n) {
  if (!ctx || (!paths && n))
    return 0;
  return ctx->Impl.invalidate(llvm::ArrayRef<const char *>(paths, n));
}

extern "C" void fbsd_stats(fbsd_ctx *ctx, fbsd_stats_out *out) {
  if (ctx && out)
    ctx->Impl.stats(*out);
}
```

`fbsd.exports`:

```
fbsd_api_version
fbsd_create
fbsd_destroy
fbsd_scan
fbsd_invalidate
fbsd_stats
```

Add `InternTable.cpp InternConsumer.cpp Context.cpp` to `fbsdCore`.

- [ ] **Step 8: Extend the CLI** with the full `Api`, `run`, and path printing. Replace the `Api` struct and `loadApi` body, and add `cmdRun`:

```cpp
struct Api {
  uint32_t (*api_version)();
  fbsd_ctx *(*create)(const fbsd_options *);
  void (*destroy)(fbsd_ctx *);
  int (*scan)(fbsd_ctx *, const char *, const char *const *, uint32_t, fbsd_result *,
              const char **);
  uint32_t (*invalidate)(fbsd_ctx *, const char *const *, uint32_t);
  void (*stats)(fbsd_ctx *, fbsd_stats_out *);
};
```

In `loadApi`, after `api_version`:

```cpp
  A.create = reinterpret_cast<decltype(A.create)>(Get("fbsd_create"));
  A.destroy = reinterpret_cast<decltype(A.destroy)>(Get("fbsd_destroy"));
  A.scan = reinterpret_cast<decltype(A.scan)>(Get("fbsd_scan"));
  A.invalidate = reinterpret_cast<decltype(A.invalidate)>(Get("fbsd_invalidate"));
  A.stats = reinterpret_cast<decltype(A.stats)>(Get("fbsd_stats"));
```

`run` implementation (add includes `llvm/Support/JSON.h`, `llvm/Support/MemoryBuffer.h`, `llvm/ADT/StringMap.h`, `<set>`, `<thread>`):

```cpp
struct RunOpts {
  StringMap<std::string> Defines;
  std::string RelativeTo;
};

std::string expand(StringRef S, const RunOpts &O) {
  std::string Out;
  while (!S.empty()) {
    size_t P = S.find("${");
    size_t E = P == StringRef::npos ? StringRef::npos : S.find('}', P);
    if (E == StringRef::npos) {
      Out += S;
      break;
    }
    Out += S.substr(0, P);
    StringRef Name = S.slice(P + 2, E);
    auto It = O.Defines.find(Name);
    Out += It == O.Defines.end() ? ("${" + Name + "}").str() : It->second;
    S = S.substr(E + 1);
  }
  return Out;
}

std::string display(StringRef Path, const RunOpts &O) {
  std::string P = Path.str();
  std::replace(P.begin(), P.end(), '\\', '/');
  std::string Base = O.RelativeTo;
  std::replace(Base.begin(), Base.end(), '\\', '/');
  if (!Base.empty() && StringRef(P).starts_with_insensitive(Base + "/"))
    return "T/" + P.substr(Base.size() + 1);
  return P;
}

std::vector<std::string> strings(const json::Array *A, const RunOpts &O) {
  std::vector<std::string> Out;
  if (A)
    for (const json::Value &V : *A)
      Out.push_back(expand(V.getAsString().value_or(""), O));
  return Out;
}

std::vector<const char *> cstrs(const std::vector<std::string> &V) {
  std::vector<const char *> Out;
  for (const std::string &S : V)
    Out.push_back(S.c_str());
  return Out;
}

void printScan(int RC, const fbsd_result &R, const char *Diag, const RunOpts &O) {
  outs() << "scan rc=" << RC << " flags="
         << ((R.flags & FBSD_RESULT_INEXACT) ? "inexact" : "exact")
         << ((R.flags & FBSD_RESULT_MEMO_HIT) ? ",memo" : "") << " count=" << R.count
         << "\n";
  for (uint32_t I = 0; I != R.count; ++I)
    outs() << "  " << display(R.files[I], O) << "\n";
  if (RC && Diag) {
    SmallVector<StringRef, 8> Lines;
    StringRef(Diag).split(Lines, '\n', -1, false);
    for (StringRef L : Lines)
      outs() << "  diag: " << L.rtrim() << "\n";
  }
}

int cmdRun(const Api &A, ArrayRef<StringRef> Args, RunOpts O) {
  if (Args.empty()) {
    errs() << "run: missing script\n";
    return 2;
  }
  StringRef Script = Args[0];
  for (size_t I = 1; I + 1 < Args.size(); I += 2) {
    if (Args[I] == "--define") {
      auto [K, V] = Args[I + 1].split('=');
      O.Defines[K] = V.str();
    } else if (Args[I] == "--relative-to") {
      O.RelativeTo = Args[I + 1].str();
    }
  }
  auto Buf = MemoryBuffer::getFile(Script);
  if (!Buf) {
    errs() << "run: cannot read " << Script << "\n";
    return 2;
  }
  fbsd_options Opts = {sizeof(fbsd_options), 0, 0};
  fbsd_ctx *Ctx = A.create(&Opts);
  SmallVector<StringRef, 16> Lines;
  (*Buf)->getBuffer().split(Lines, '\n', -1, false);
  for (StringRef Line : Lines) {
    Line = Line.trim();
    if (Line.empty())
      continue;
    Expected<json::Value> V = json::parse(Line);
    if (!V) {
      errs() << "run: bad json: " << toString(V.takeError()) << "\n";
      return 2;
    }
    const json::Object &Obj = *V->getAsObject();
    StringRef Op = Obj.getString("op").value_or("");
    if (Op == "scan") {
      std::string Cwd = expand(Obj.getString("cwd").value_or(""), O);
      auto Argv = strings(Obj.getArray("argv"), O);
      auto C = cstrs(Argv);
      fbsd_result R = {};
      const char *Diag = nullptr;
      int RC = A.scan(Ctx, Cwd.c_str(), C.data(), C.size(), &R, &Diag);
      printScan(RC, R, Diag, O);
    } else if (Op == "scan-parallel") {
      std::string Cwd = expand(Obj.getString("cwd").value_or(""), O);
      auto Argv = strings(Obj.getArray("argv"), O);
      auto C = cstrs(Argv);
      int N = static_cast<int>(Obj.getInteger("threads").value_or(8));
      std::vector<fbsd_result> Rs(N);
      std::vector<int> RCs(N);
      std::vector<std::thread> Ts;
      for (int I = 0; I != N; ++I)
        Ts.emplace_back([&, I] {
          const char *Diag = nullptr;
          RCs[I] = A.scan(Ctx, Cwd.c_str(), C.data(), C.size(), &Rs[I], &Diag);
        });
      for (auto &T : Ts)
        T.join();
      std::set<const void *> Distinct;
      int RC = 0;
      for (int I = 0; I != N; ++I) {
        Distinct.insert(Rs[I].files);
        RC = RC ? RC : RCs[I];
      }
      outs() << "scan-parallel rc=" << RC << " distinct_results=" << Distinct.size()
             << " count=" << Rs[0].count << "\n";
    } else if (Op == "invalidate") {
      auto Paths = strings(Obj.getArray("paths"), O);
      auto C = cstrs(Paths);
      outs() << "invalidate dropped=" << A.invalidate(Ctx, C.data(), C.size()) << "\n";
    } else if (Op == "stats") {
      fbsd_stats_out S = {};
      S.struct_size = sizeof(S);
      A.stats(Ctx, &S);
      outs() << "stats scans=" << S.scans << " memo_hits=" << S.memo_hits
             << " invalidated=" << S.invalidated_entries
             << " service_resets=" << S.service_resets << "\n";
    } else {
      errs() << "run: unknown op " << Op << "\n";
      return 2;
    }
    outs().flush();
  }
  A.destroy(Ctx);
  return 0;
}
```

In `main`, before the unknown-command error:

```cpp
  if (Args[0] == "run") {
    RunOpts O;
    O.Defines["BIN"] = sys::path::parent_path(Dll).str();
    return cmdRun(A, ArrayRef<StringRef>(Args).drop_front(), O);
  }
```

`${BIN}` expands with backslashes, and paths like `${BIN}/clang-cl.exe` mix separators. The Windows driver accepts that, and `detectDriver` uses `sys::path::filename`, which handles both.

- [ ] **Step 9: Build and run all fbsd tests**

```bash
cmd //c 'D:\scratch\fbsd\check.bat E:\code\llvm-project\clang\test\FastbuildScanDeps' 2>&1 | tee /d/scratch/fbsd/task4.log | tail -20
```

Expected: every test PASS. `cl-translate.c` is UNSUPPORTED unless run from the VS environment; run it once with `FBSD_TEST_CL` set to the cl.exe from the Environment section, and it must PASS.

Likely failures and what they mean:
- `include-forms.c` missing `T/inc/probe.h`: the `__has_include` path isn't reported (Review Focus 2). `ModuleDepCollectorPP::HasInclude` does report it, so check that `EmitWarnings=false` or the adapter didn't drop it. Don't loosen the test.
- `T/inc/forced.h` not second: forced includes are entered after the main file, so the source-first order holds. If the order differs, fix `InternConsumer` to put the file whose path matches the `/Tp`/positional source first, and keep the test.
- `dedup-spellings.c` count 3+: de-dup by UID failed. Check that `FS.status` returns the same UniqueID for all spellings.

- [ ] **Step 10: Commit**

```bash
git add -A clang/tools/fastbuild-scan-deps clang/test/FastbuildScanDeps
git commit -m "[fbsd] Scan path: Context, interned real paths, C API, fbsd-cli run

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Memo

**Files:**
- Modify: `Context.h`, `Context.cpp`
- Create: `clang/test/FastbuildScanDeps/memo.c`, `memo-parallel.c`

**Interfaces:**
- Consumes: `appendMemoKey`, `ConcurrentTable`, `ThreadState::KeyBuf`.
- Produces: `struct fbsd::MemoEntry { uint64_t Hash; uint64_t Hash2; const char *Key; uint32_t KeyLen; const char *const *Files; uint32_t Count; uint32_t Flags; };`, member `ConcurrentTable<MemoEntry> Memo;`, results flagged `FBSD_RESULT_MEMO_HIT` on hit. Task 6 relies on `MemoEntry::Files` pointers being the interned `InternEntry::Path` pointers.

- [ ] **Step 1: Write the failing lit tests**

`memo.c`:

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK:      scan rc=0 flags=exact count=2
// CHECK:      scan rc=0 flags=exact,memo count=2
// CHECK-NEXT:   T/a.cpp
// CHECK-NEXT:   T/h.h
// CHECK:      scan rc=0 flags=exact count=2
// CHECK:      scan rc=0 flags=exact count=2
// CHECK:      scan rc=3
// CHECK:      scan rc=3
// CHECK:      stats scans=6 memo_hits=1

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","/DX","a.cpp"]}
{"op":"scan","cwd":"${T}/sub/..","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","bad.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","bad.cpp"]}
{"op":"stats"}
//--- a.cpp
#include "h.h"
//--- h.h
//--- bad.cpp
#include "missing.h"
//--- sub/keep
```

The 4th scan has a different cwd spelling, so it's a separate memo key. That's accepted; the key is lexical. Errors are not memoised, so both `bad.cpp` scans run.

`memo-parallel.c` (Review Focus 4):

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK: scan-parallel rc=0 distinct_results=1 count=2
// CHECK: scan-parallel rc=0 distinct_results=1 count=2

//--- script.jsonl
{"op":"scan-parallel","threads":32,"cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
{"op":"scan-parallel","threads":32,"cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
//--- a.cpp
#include "h.h"
//--- h.h
```

`distinct_results=1` in the first line needs the losers of a concurrent insert to return the winner's entry, not their own array.

- [ ] **Step 2: Run to verify failure**

```bash
cmd //c 'D:\scratch\fbsd\check.bat E:\code\llvm-project\clang\test\FastbuildScanDeps\memo.c E:\code\llvm-project\clang\test\FastbuildScanDeps\memo-parallel.c' 2>&1 | tail -15
```

Expected: FAIL. No `,memo` flag, and `distinct_results` > 1.

- [ ] **Step 3: Implement the memo.** In `Context.h` add:

```cpp
struct MemoEntry {
  uint64_t Hash;
  uint64_t Hash2;
  const char *Key;
  uint32_t KeyLen;
  const char *const *Files;
  uint32_t Count;
  uint32_t Flags;
};
```

and the member `ConcurrentTable<MemoEntry> Memo;`, constructed as `Memo(std::max<size_t>(Opts.expected_tus, 1024))` in `Context::Context`.

In `Context::scan`, after `expandResponseFiles` and before `adaptCommand`:

```cpp
  TS.KeyBuf.clear();
  appendMemoKey(Cwd, IncludeEnv, ExternalIncludeEnv, Args, TS.KeyBuf);
  XXH128_hash_t H = xxh3_128bits(reinterpret_cast<const uint8_t *>(TS.KeyBuf.data()),
                                 TS.KeyBuf.size());
  StringRef Key = TS.KeyBuf;
  auto SameKey = [&](const MemoEntry &E) {
    return E.Hash2 == H.high64 && StringRef(E.Key, E.KeyLen) == Key;
  };
  if (const MemoEntry *E = Memo.find(H.low64, SameKey)) {
    Out = {E->Files, E->Count, E->Flags | FBSD_RESULT_MEMO_HIT};
    MemoHits.fetch_add(1, std::memory_order_relaxed);
    addHist(MemoHitNs, Start);
    return FBSD_OK;
  }
```

At the end of a successful `scanUncached` (replace the `Out = ...; return FBSD_OK;` tail), with `H` and `Key` passed in as new parameters:

```cpp
  char *KeyCopy = TS.Arena.Allocate<char>(Key.size());
  std::memcpy(KeyCopy, Key.data(), Key.size());
  auto *E = new (TS.Arena.Allocate<MemoEntry>()) MemoEntry{
      H.low64, H.high64, KeyCopy, static_cast<uint32_t>(Key.size()), Files,
      static_cast<uint32_t>(TS.Files.size()), Cmd.Inexact ? FBSD_RESULT_INEXACT : 0u};
  const MemoEntry *Winner = Memo.insert(E, [&](const MemoEntry &O) {
    return O.Hash2 == H.high64 && StringRef(O.Key, O.KeyLen) == Key;
  });
  Out = {Winner->Files, Winner->Count, Winner->Flags};
  return FBSD_OK;
```

Change the `scanUncached` signature to `int scanUncached(ThreadState &TS, StringRef Cwd, const AdaptedCommand &Cmd, XXH128_hash_t H, StringRef Key, fbsd_result &Out, const char *&Diag)`, and include `llvm/Support/xxhash.h` in `Context.h`.

The scan itself runs `computeDependencies`, which does not touch `TS.KeyBuf`, so `Key` stays valid across it.

In `stats`, fill `memo_entries = Memo.size()` and `arena_bytes` (sum `TS->Arena.getTotalMemory()` over `Threads` under `ThreadsLock`; make `ThreadsLock` `mutable`).

- [ ] **Step 4: Unit-test memo-hit latency.** Create `clang/unittests/FastbuildScanDeps/ContextTest.cpp`, add it to the unittest CMakeLists, and add `clangDependencyScanning clangTooling` via `CLANG_LIBS` if the link needs them (`fbsdCore` is PUBLIC, so it shouldn't).

```cpp
#include "Context.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Program.h"
#include "llvm/Support/raw_ostream.h"
#include "gtest/gtest.h"
#include <chrono>

using namespace llvm;

namespace {
std::string writeTree(SmallString<128> &Dir) {
  EXPECT_FALSE(sys::fs::createUniqueDirectory("fbsd-ctx", Dir));
  auto Write = [&](StringRef Name, StringRef Text) {
    SmallString<128> P(Dir);
    sys::path::append(P, Name);
    std::error_code EC;
    raw_fd_ostream OS(P, EC);
    OS << Text;
  };
  Write("a.cpp", "#include \"h.h\"\n");
  Write("h.h", "");
  SmallString<128> Src(Dir);
  sys::path::append(Src, "a.cpp");
  return std::string(Src);
}

std::string clangCL() {
  std::string Self = sys::fs::getMainExecutable(nullptr, (void *)&clangCL);
  SmallString<260> P(Self);
  while (!P.empty() && sys::path::filename(P) != "tools")
    sys::path::remove_filename(P);
  sys::path::remove_filename(P);
  sys::path::append(P, "bin", "clang-cl.exe");
  return std::string(P);
}
} // namespace

TEST(FbsdContext, MemoHitIsFast) {
  SmallString<128> Dir;
  std::string Src = writeTree(Dir);
  std::string Exe = clangCL();
  if (!sys::fs::exists(Exe))
    GTEST_SKIP() << "no clang-cl at " << Exe;
  fbsd_options O = {sizeof(O), 0, 0};
  fbsd::Context C(O);
  std::vector<std::string> Store{Exe, "/c"};
  for (int I = 0; I != 162; ++I)
    Store.push_back(("/IC:\\src\\project\\Code\\Module" +
                     Twine(I) + "\\Public")
                        .str());
  Store.push_back(Src);
  std::vector<const char *> Argv;
  for (auto &S : Store)
    Argv.push_back(S.c_str());
  fbsd_result R;
  const char *Diag = nullptr;
  ASSERT_EQ(C.scan(Dir, Argv, R, Diag), FBSD_OK) << (Diag ? Diag : "");
  constexpr int N = 10000;
  auto Start = std::chrono::steady_clock::now();
  for (int I = 0; I != N; ++I)
    ASSERT_EQ(C.scan(Dir, Argv, R, Diag), FBSD_OK);
  auto Ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - Start)
                .count() /
            N;
  EXPECT_TRUE(R.flags & FBSD_RESULT_MEMO_HIT);
  EXPECT_LT(Ns, 5000) << "memo hit took " << Ns << " ns";
  sys::fs::remove_directories(Dir);
}
```

The `clangCL()` helper walks up from `...\tools\clang\unittests\FastbuildScanDeps\` to the build root. If the layout differs, adjust it so it finds `<build>\bin\clang-cl.exe`.

- [ ] **Step 5: Run the tests**

```bash
cmd //c 'D:\scratch\fbsd\check.bat E:\code\llvm-project\clang\test\FastbuildScanDeps' 2>&1 | tail -15
/e/code/llvm-project-build/fbsd/tools/clang/unittests/FastbuildScanDeps/FastbuildScanDepsTests.exe
```

Expected: all PASS. If `MemoHitIsFast` misses 5 µs, profile it before changing the target. The hot path is building `KeyBuf` (no allocation after the first call), one xxh3 pass, and one memcmp.

- [ ] **Step 6: Commit**

```bash
git add -A clang/tools/fastbuild-scan-deps clang/test/FastbuildScanDeps clang/unittests/FastbuildScanDeps
git commit -m "[fbsd] Memoise scan results by full command key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Invalidation

**Files:**
- Modify: `clang/include/clang/DependencyScanning/DependencyScanningFilesystem.h`, `clang/lib/DependencyScanning/DependencyScanningFilesystem.cpp`
- Modify: `clang/unittests/DependencyScanning/DependencyScanningFilesystemTest.cpp`
- Modify: `Context.h`, `Context.cpp`
- Create: `clang/test/FastbuildScanDeps/invalidate.c`

**Interfaces:**
- Produces (clang): `void DependencyScanningFilesystemSharedCache::forEachCachedFilename(llvm::function_ref<void(llvm::StringRef Filename)> Fn) const;`. It locks each shard in turn.
- Produces (fbsd): `Context::invalidate` returns memo entries dropped plus FS-cache entries matched. If any FS-cache entry matched, it clears the whole memo and publishes a new `ServiceSlot` (Gen + 1), and workers rebuild on their next scan. The memo has to be cleared because an entry doesn't record the missing files it depended on: in `invalidate.c`, `probe.cpp`'s memo entry lists `fallback.h`, not `late.h`.

- [ ] **Step 1: Write the failing clang unit test.** Append to `DependencyScanningFilesystemTest.cpp`, following the file's existing `InMemoryFileSystem` setup:

```cpp
TEST(DependencyScanningFilesystem, ForEachCachedFilename) {
  auto InMemoryFS = llvm::makeIntrusiveRefCnt<llvm::vfs::InMemoryFileSystem>();
  InMemoryFS->setCurrentWorkingDirectory("/");
  InMemoryFS->addFile("/a.h", 0, llvm::MemoryBuffer::getMemBuffer(""));
  DependencyScanningServiceOptions Opts;
  Opts.MakeVFS = [&] { return InMemoryFS; };
  Opts.CacheNegativeStats = true;
  DependencyScanningService Service(std::move(Opts));
  DependencyScanningWorkerFilesystem DepFS(Service, InMemoryFS);
  (void)DepFS.status("/a.h");
  (void)DepFS.status("/missing.h");
  std::vector<std::string> Names;
  Service.getSharedCache().forEachCachedFilename(
      [&](StringRef N) { Names.push_back(N.str()); });
  EXPECT_TRUE(llvm::is_contained(Names, "/a.h"));
  EXPECT_TRUE(llvm::is_contained(Names, "/missing.h"));
}
```

Match the existing tests' construction of `DependencyScanningService` and `DependencyScanningWorkerFilesystem` if the constructor arguments differ.

- [ ] **Step 2: Run to verify failure**

```bash
cmd //c 'D:\scratch\fbsd\build.bat ClangDependencyScanningTests' 2>&1 | tail -5
```

Expected: compile error, no member `forEachCachedFilename`. If the unified `AllClangUnitTests` binary owns these tests, build `AllClangUnitTests` and filter with `--gtest_filter=DependencyScanningFilesystem.ForEachCachedFilename`.

- [ ] **Step 3: Implement it in clang.** Declare it in the public section of `DependencyScanningFilesystemSharedCache` (next to `getOutOfDateEntries`):

```cpp
  void forEachCachedFilename(
      llvm::function_ref<void(llvm::StringRef Filename)> Fn) const;
```

Define it in `DependencyScanningFilesystem.cpp`:

```cpp
void DependencyScanningFilesystemSharedCache::forEachCachedFilename(
    llvm::function_ref<void(StringRef)> Fn) const {
  for (unsigned I = 0; I < NumShards; ++I) {
    const CacheShard &Shard = CacheShards[I];
    std::lock_guard<std::mutex> LockGuard(Shard.CacheLock);
    for (const auto &Entry : Shard.CacheByFilename)
      Fn(Entry.getKey());
  }
}
```

Run the test; expected PASS.

- [ ] **Step 4: Write the failing lit test** `invalidate.c`

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// Unrelated path: nothing dropped, nothing reset.
// CHECK:      scan rc=0 flags=exact count=2
// CHECK:      invalidate dropped=0
// CHECK:      scan rc=0 flags=exact,memo count=2
// A dependency: its memo entry and FS entry go.
// CHECK:      invalidate dropped={{[2-9]|[1-9][0-9]+}}
// CHECK:      scan rc=0 flags=exact count=2
// A negatively cached header that now exists is found after invalidation.
// CHECK:      scan rc=0 flags=exact count=2
// CHECK-NEXT:   T/probe.cpp
// CHECK-NEXT:   T/fallback.h
// CHECK:      invalidate dropped={{[1-9][0-9]*}}
// CHECK:      scan rc=0 flags=exact count=2
// CHECK-NEXT:   T/probe.cpp
// CHECK-NEXT:   T/inc/late.h
// CHECK:      stats {{.*}} service_resets={{[1-9]}}

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
{"op":"invalidate","paths":["${T}/unrelated.h"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
{"op":"invalidate","paths":["${T}/h.h"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","a.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","/Iinc","probe.cpp"]}
{"op":"copy","from":"${T}/late.src","to":"${T}/inc/late.h"}
{"op":"invalidate","paths":["${T}/inc/late.h"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","/Iinc","probe.cpp"]}
{"op":"stats"}
//--- a.cpp
#include "h.h"
//--- h.h
//--- probe.cpp
#if __has_include(<late.h>)
#include <late.h>
#else
#include "fallback.h"
#endif
//--- fallback.h
//--- late.src
//--- inc/keep
```

This needs a `copy` op in `fbsd-cli run`. Add it to `cmdRun`:

```cpp
    } else if (Op == "copy") {
      std::string From = expand(Obj.getString("from").value_or(""), O);
      std::string To = expand(Obj.getString("to").value_or(""), O);
      if (std::error_code EC = sys::fs::copy_file(From, To)) {
        errs() << "run: copy failed: " << EC.message() << "\n";
        return 2;
      }
```

- [ ] **Step 5: Run to verify failure**

Expected: FAIL at the second `invalidate dropped=` (it's 0).

- [ ] **Step 6: Implement `Context::invalidate`**

```cpp
static std::string normalizeForMatch(StringRef P) {
  SmallString<256> S(P);
  sys::path::remove_dots(S, /*remove_dot_dot=*/true);
  std::string Out = S.str().lower();
  std::replace(Out.begin(), Out.end(), '/', '\\');
  return Out;
}

uint32_t Context::invalidate(ArrayRef<const char *> Paths) {
  InvalidateCalls.fetch_add(1, std::memory_order_relaxed);
  if (Paths.empty())
    return 0;
  std::lock_guard<std::mutex> L(InvalidateLock);

  StringSet<> Wanted;
  for (const char *P : Paths) {
    SmallString<256> Abs(P);
    sys::fs::make_absolute(Abs);
    Wanted.insert(normalizeForMatch(Abs));
    SmallString<256> Real;
    if (!sys::fs::real_path(P, Real))
      Wanted.insert(normalizeForMatch(Real));
  }

  DenseSet<const char *> Interned;
  Interns.forEach([&](const InternEntry &E) {
    if (Wanted.contains(normalizeForMatch(E.Path)))
      Interned.insert(E.Path);
  });
  size_t MemoDropped = Interned.empty() ? 0 : Memo.removeIf([&](const MemoEntry &E) {
    return any_of(ArrayRef(E.Files, E.Count),
                  [&](const char *F) { return Interned.contains(F); });
  });

  size_t FsMatched = 0;
  ServiceSlot *Slot = CurrentService.load(std::memory_order_acquire);
  Slot->Service.getSharedCache().forEachCachedFilename([&](StringRef Name) {
    if (Wanted.contains(normalizeForMatch(Name)))
      ++FsMatched;
  });
  if (FsMatched) {
    // Memo entries don't record the misses they depended on, so none survive a reset.
    MemoDropped += Memo.removeIf([](const MemoEntry &) { return true; });
    auto Fresh = std::make_unique<ServiceSlot>(Slot->Gen + 1);
    ServiceSlot *Raw = Fresh.get();
    {
      std::lock_guard<std::mutex> SL(ServicesLock);
      Services.push_back(std::move(Fresh));
    }
    CurrentService.store(Raw, std::memory_order_release);
    ServiceResets.fetch_add(1, std::memory_order_relaxed);
  }

  uint32_t Dropped = static_cast<uint32_t>(MemoDropped + FsMatched);
  InvalidatedEntries.fetch_add(Dropped, std::memory_order_relaxed);
  return Dropped;
}
```

Add `std::mutex InvalidateLock;` to `Context`, and include `llvm/ADT/DenseSet.h` and `llvm/ADT/StringSet.h`. Old `ServiceSlot`s stay alive in `Services` until destruction, because a worker on another thread may still be scanning with one.

The negative-cache case works because the negatively cached spelling of `inc/late.h` normalises to the same string as the invalidated path.

- [ ] **Step 7: Run all tests**

```bash
cmd //c 'D:\scratch\fbsd\check.bat E:\code\llvm-project\clang\test\FastbuildScanDeps' 2>&1 | tail -15
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add -A clang/include/clang/DependencyScanning clang/lib/DependencyScanning clang/unittests/DependencyScanning clang/tools/fastbuild-scan-deps clang/test/FastbuildScanDeps
git commit -m "[fbsd] Add fbsd_invalidate with shared-cache reset

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Crash handling and poisoning

**Files:**
- Modify: `Context.h`, `Context.cpp`, `fbsd.cpp`
- Create: `clang/test/FastbuildScanDeps/crash.c`

**Interfaces:**
- Produces: a crash inside a scan returns `FBSD_E_CRASH`, with a diag containing `fbsd: scan crashed` and the command line. Every later call returns `FBSD_E_CRASH`. `fbsd_destroy` on a poisoned context leaks it on purpose. Test hook: the env var `FBSD_TEST_CRASH_ON=<substring>`, read at `fbsd_create`, crashes any scan whose joined command line contains the substring.

- [ ] **Step 1: Write the failing lit test** `crash.c`

```c
// RUN: rm -rf %t && split-file %s %t
// RUN: env FBSD_TEST_CRASH_ON=boom.cpp fbsd-cli run %t/script.jsonl --define T=%t --relative-to %t | FileCheck %s

// CHECK:      scan rc=0 flags=exact count=1
// CHECK:      scan rc=4 flags=exact count=0
// CHECK-NEXT:   diag: fbsd: scan crashed
// CHECK-NEXT:   diag: command: {{.*}}boom.cpp
// CHECK:      scan rc=4 flags=exact count=0
// CHECK-NEXT:   diag: fbsd: context is poisoned by an earlier crash

//--- script.jsonl
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","ok.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","boom.cpp"]}
{"op":"scan","cwd":"${T}","argv":["${BIN}/clang-cl.exe","/c","ok.cpp"]}
//--- ok.cpp
//--- boom.cpp
```

The third scan is a memo hit candidate, but poisoning is checked first, so it must still return 4.

- [ ] **Step 2: Run to verify failure**

Expected: the CLI process dies with an access violation, and FileCheck fails.

- [ ] **Step 3: Implement it.** In `Context.h` add `std::string TestCrashOn;` to `Context`. In the constructor:

```cpp
  TestCrashOn = sys::Process::GetEnv("FBSD_TEST_CRASH_ON").value_or("");
  CrashRecoveryContext::Enable();
```

`CrashRecoveryContext::Enable()` only sets a process-wide flag on MSVC builds; the recovery itself is SEH `__try` in `RunSafely`.

In `scanUncached`, replace the direct `computeDependencies` call:

```cpp
  bool Ok = false;
  CrashRecoveryContext CRC;
  bool Survived = CRC.RunSafely([&] {
    if (!TestCrashOn.empty() &&
        any_of(Cmd.Args, [&](const std::string &A) { return StringRef(A).contains(TestCrashOn); }))
      *static_cast<volatile int *>(nullptr) = 0;
    Ok = clang::tooling::computeDependencies(W, Cwd, Cmd.Args, Consumer, Controller,
                                             DiagPrinter);
  });
  if (!Survived) {
    Poisoned.store(true, std::memory_order_release);
    TS.Worker.release();
    return fail(TS, FBSD_E_CRASH,
                "fbsd: scan crashed\ncommand: " + join(Cmd.Args, " "), Diag);
  }
```

The hook compares against `Cmd.Args`, so the stripped and rewritten argv is what's matched. Include `llvm/Support/CrashRecoveryContext.h` and `llvm/ADT/StringExtras.h`.

`TS.Worker.release()` leaks the worker, since its state may be corrupt. In `fbsd.cpp`:

```cpp
extern "C" void fbsd_destroy(fbsd_ctx *ctx) {
  if (ctx && ctx->Impl.poisoned())
    return; // A crashed scan may have corrupted the heap; don't run destructors.
  delete ctx;
}
```

Add `bool poisoned() const { return Poisoned.load(std::memory_order_acquire); }` to `Context`.

- [ ] **Step 4: Run all tests**

```bash
cmd //c 'D:\scratch\fbsd\check.bat E:\code\llvm-project\clang\test\FastbuildScanDeps' 2>&1 | tail -15
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add -A clang/tools/fastbuild-scan-deps clang/test/FastbuildScanDeps
git commit -m "[fbsd] Catch scan crashes and poison the context

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Benchmark, golden comparison, and results

**Files:**
- Modify: `cli/FbsdCli.cpp` (the `bench` command)
- Create: `clang/tools/fastbuild-scan-deps/utils/fbsd_golden.py`
- Modify: `E:\p4\docs\plans\FASTBuild distribution improvements.md` (record results under fix 4)

**Interfaces:**
- Produces:
  - `fbsd-cli bench COMPDB [-j N] [--repeat N] [--dump FILE]`. Each pass prints `pass <i> wall=<s>s scans=<n> memo_hits=<n> errors=<n>`, then one `stats` line: `interned=<n> memo_entries=<n> arena_mb=<n> private_mb=<n> memo_hit_p50_ns=<n> scan_p50_us=<n>`.
  - `--dump FILE` writes pass 1 results as JSON: `{"<file>": ["<dep>", ...]}`. Failures are written as `{"<file>": {"error": "<diag>"}}`.
  - `fbsd_golden.py COMPDB DUMP [-j N] [--limit N]`. It runs each entry's compiler with `/showIncludes /E` (output to NUL), parses the `Note: including file:` lines, compares sets after `os.path.realpath` + `normcase`, and prints `tus=<n> match=<n> mismatch=<n>` plus the first 20 mismatches with missing and extra files.

- [ ] **Step 1: Implement `bench`** in `FbsdCli.cpp`

```cpp
struct Job {
  std::string Cwd, File;
  std::vector<std::string> Argv;
};

int cmdBench(const Api &A, ArrayRef<StringRef> Args) {
  if (Args.empty()) {
    errs() << "bench: missing compdb\n";
    return 2;
  }
  unsigned Threads = std::thread::hardware_concurrency(), Repeat = 2;
  std::string Dump;
  for (size_t I = 1; I + 1 < Args.size(); I += 2) {
    if (Args[I] == "-j")
      Args[I + 1].getAsInteger(10, Threads);
    else if (Args[I] == "--repeat")
      Args[I + 1].getAsInteger(10, Repeat);
    else if (Args[I] == "--dump")
      Dump = Args[I + 1].str();
  }
  auto Buf = MemoryBuffer::getFile(Args[0]);
  if (!Buf) {
    errs() << "bench: cannot read " << Args[0] << "\n";
    return 2;
  }
  Expected<json::Value> DB = json::parse((*Buf)->getBuffer());
  if (!DB || !DB->getAsArray()) {
    errs() << "bench: bad compdb\n";
    return 2;
  }
  std::vector<Job> Jobs;
  for (const json::Value &V : *DB->getAsArray()) {
    const json::Object &O = *V.getAsObject();
    Job J;
    J.Cwd = O.getString("directory").value_or("").str();
    J.File = O.getString("file").value_or("").str();
    for (const json::Value &Arg : *O.getArray("arguments"))
      J.Argv.push_back(Arg.getAsString().value_or("").str());
    Jobs.push_back(std::move(J));
  }

  fbsd_options Opts = {sizeof(fbsd_options), 0, static_cast<uint32_t>(Jobs.size())};
  fbsd_ctx *Ctx = A.create(&Opts);
  std::vector<fbsd_result> Results(Jobs.size());
  std::vector<std::string> Diags(Jobs.size());
  for (unsigned Pass = 1; Pass <= Repeat; ++Pass) {
    std::atomic<size_t> Next{0}, Errors{0}, Hits{0};
    auto Start = std::chrono::steady_clock::now();
    std::vector<std::thread> Ts;
    for (unsigned T = 0; T != Threads; ++T)
      Ts.emplace_back([&] {
        for (size_t I; (I = Next.fetch_add(1)) < Jobs.size();) {
          auto C = cstrs(Jobs[I].Argv);
          const char *Diag = nullptr;
          int RC = A.scan(Ctx, Jobs[I].Cwd.c_str(), C.data(), C.size(), &Results[I], &Diag);
          if (RC) {
            Errors.fetch_add(1);
            if (Pass == 1)
              Diags[I] = Diag ? Diag : "";
          } else if (Results[I].flags & FBSD_RESULT_MEMO_HIT) {
            Hits.fetch_add(1);
          }
        }
      });
    for (auto &T : Ts)
      T.join();
    double Wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - Start).count();
    outs() << formatv("pass {0} wall={1:f2}s scans={2} memo_hits={3} errors={4}\n", Pass,
                      Wall, Jobs.size(), Hits.load(), Errors.load());
    if (Pass == 1 && !Dump.empty()) {
      json::Object Out;
      for (size_t I = 0; I != Jobs.size(); ++I) {
        if (!Diags[I].empty()) {
          Out[Jobs[I].File] = json::Object{{"error", Diags[I]}};
          continue;
        }
        json::Array Files;
        for (uint32_t K = 0; K != Results[I].count; ++K)
          Files.push_back(Results[I].files[K]);
        Out[Jobs[I].File] = std::move(Files);
      }
      std::error_code EC;
      raw_fd_ostream OS(Dump, EC);
      OS << json::Value(std::move(Out));
    }
  }
  fbsd_stats_out S = {};
  S.struct_size = sizeof(S);
  A.stats(Ctx, &S);
  auto P50 = [](const uint64_t *H) {
    uint64_t Total = 0, Seen = 0;
    for (int I = 0; I != FBSD_HIST_BUCKETS; ++I)
      Total += H[I];
    for (int I = 0; I != FBSD_HIST_BUCKETS; ++I)
      if ((Seen += H[I]) * 2 >= Total && Total)
        return uint64_t(1) << I;
    return uint64_t(0);
  };
  outs() << formatv("stats interned={0} memo_entries={1} arena_mb={2} private_mb={3} "
                    "memo_hit_p50_ns={4} scan_p50_us={5}\n",
                    S.interned_files, S.memo_entries, S.arena_bytes >> 20,
                    S.process_private_bytes >> 20, P50(S.memo_hit_ns),
                    P50(S.scan_ns) / 1000);
  A.destroy(Ctx);
  return 0;
}
```

Add `if (Args[0] == "bench") return cmdBench(A, ArrayRef<StringRef>(Args).drop_front());` to `main`, and include `<atomic>`, `<chrono>` and `llvm/Support/FormatVariadic.h`. `hardware_concurrency()` returns an `unsigned`; `getAsInteger` into `unsigned` works as written.

- [ ] **Step 2: Write `utils/fbsd_golden.py`**

```python
"""Compare fbsd-cli bench --dump output with the compiler's /showIncludes."""
import argparse
import concurrent.futures
import json
import os
import subprocess

PREFIX = "Note: including file:"


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def strip_pch(args):
    out = []
    for a in args:
        if len(a) > 2 and a[0] in "/-" and a[1:3] in ("Yu", "Yc", "Fp"):
            continue
        out.append(a)
    return out


def show_includes(entry):
    args = strip_pch(entry["arguments"])
    args = [a for a in args if not (a[:3] in ("/Fo", "-Fo") or a in ("/c", "-c"))]
    args += ["/showIncludes", "/E"]
    p = subprocess.run(args, cwd=entry["directory"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True, errors="replace")
    deps = {norm(entry["file"])}
    for line in p.stderr.splitlines():
        if line.startswith(PREFIX):
            deps.add(norm(line[len(PREFIX):].strip()))
    return entry["file"], p.returncode, deps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("compdb")
    ap.add_argument("dump")
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    entries = json.load(open(a.compdb))
    if a.limit:
        entries = entries[: a.limit]
    dump = json.load(open(a.dump, encoding="utf-8"))
    match = mismatch = 0
    reports = []
    with concurrent.futures.ThreadPoolExecutor(a.j) as ex:
        for file, rc, want in ex.map(show_includes, entries):
            got = dump.get(file)
            if rc != 0 or not isinstance(got, list):
                mismatch += 1
                reports.append((file, f"rc={rc} fbsd={'error' if isinstance(got, dict) else 'missing'}", set(), set()))
                continue
            have = {norm(p) for p in got}
            if have == want:
                match += 1
            else:
                mismatch += 1
                reports.append((file, "diff", want - have, have - want))
    print(f"tus={match + mismatch} match={match} mismatch={mismatch}")
    for file, why, missing, extra in reports[:20]:
        print(f"{file}: {why}")
        for m in sorted(missing)[:10]:
            print(f"  missing {m}")
        for e in sorted(extra)[:10]:
            print(f"  extra   {e}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Build a non-assertions Release for benchmarking.** Assertions distort timings. Create `D:\scratch\fbsd\build-rel.bat`: a copy of `build.bat` with `B=E:\code\llvm-project-build\fbsd-rel` and `-DLLVM_ENABLE_ASSERTIONS=OFF -DLLVM_INCLUDE_TESTS=OFF -DCLANG_INCLUDE_TESTS=OFF`. Then:

```bash
cmd //c 'D:\scratch\fbsd\build-rel.bat fastbuild-scan-deps fbsd-cli clang' 2>&1 | tail -3
```

- [ ] **Step 4: Benchmark both compdbs** from the VS environment, so `INCLUDE` and `cl.exe` resolve:

```bash
S='<compdb dir>'
cmd //c "call <VS x64 setup script> >nul && E:\code\llvm-project-build\fbsd-rel\bin\fbsd-cli.exe bench $S\compdb_clang\compile_commands.json -j 48 --repeat 2 --dump D:\scratch\fbsd\dump_clang.json" | tee /d/scratch/fbsd/bench_clang.txt
cmd //c "call <VS x64 setup script> >nul && E:\code\llvm-project-build\fbsd-rel\bin\fbsd-cli.exe bench $S\compile_commands.json -j 48 --repeat 2 --dump D:\scratch\fbsd\dump_cl.json" | tee /d/scratch/fbsd/bench_cl.txt
```

Expected for the clang-cl compdb: `pass 1 wall` ≤ 8s with `errors=0`; `pass 2` has `memo_hits=2945`; `memo_hit_p50_ns` ≤ 4096. If pass 1 is over 8s, compare against `clang-scan-deps -cache-negative-stats` on the same compdb (from `statpoc\full.ps1`) before optimising. The gap is fbsd overhead (interning, real paths), and it is the thing to profile.

- [ ] **Step 5: Golden comparison**

```bash
cmd //c "call <VS x64 setup script> >nul && python E:\code\llvm-project\clang\tools\fastbuild-scan-deps\utils\fbsd_golden.py $S\compdb_clang\compile_commands.json D:\scratch\fbsd\dump_clang.json -j 48" | tee /d/scratch/fbsd/golden_clang.txt
cmd //c "call <VS x64 setup script> >nul && python E:\code\llvm-project\clang\tools\fastbuild-scan-deps\utils\fbsd_golden.py $S\compile_commands.json D:\scratch\fbsd\dump_cl.json -j 48" | tee /d/scratch/fbsd/golden_cl.txt
```

Expected:
- clang-cl: `mismatch=0`. Any mismatch is a bug to fix before shipping, since the clang-cl path claims to be exact. `/showIncludes` does not list files seen only through `__has_include`, so "extra" entries that are `__has_include` targets are expected. Confirm each such case by reading the source, and extend `fbsd_golden.py` to ignore extras that the TU names inside `__has_include`.
- cl: report the number. That's the measured inexactness of the cl bridge.

- [ ] **Step 6: Record the results.** Under "### 4. Dependency scanning instead of preprocessing" in `E:\p4\docs\plans\FASTBuild distribution improvements.md`, add a short table: cold wall, warm wall, memo-hit p50, private MB, and golden match/mismatch for clang-cl and cl. Link `fastbuild-scan-deps design.md` and this plan.

- [ ] **Step 7: Commit**

```bash
git add -A clang/tools/fastbuild-scan-deps
git commit -m "[fbsd] Add bench command and golden comparison script

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
