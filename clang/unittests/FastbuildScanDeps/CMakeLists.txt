add_distinct_clang_unittest(FastbuildScanDepsTests
  SmokeTest.cpp

  LINK_LIBS
  fbsdCore
  LLVM_COMPONENTS
  Support
  )
