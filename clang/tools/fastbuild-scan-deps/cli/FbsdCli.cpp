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
