add_llvm_library(fbsdCore STATIC BUILDTREE_ONLY PARTIAL_SOURCES_INTENDED
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
add_llvm_library(fastbuild-scan-deps SHARED BUILDTREE_ONLY PARTIAL_SOURCES_INTENDED
  fbsd.cpp

  LINK_LIBS
  fbsdCore
  )
set(LLVM_EXPORTED_SYMBOL_FILE)

add_subdirectory(cli)
