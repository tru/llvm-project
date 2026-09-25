set(LLVM_LINK_COMPONENTS Support)
add_clang_tool(fbsd-cli FbsdCli.cpp)
target_include_directories(fbsd-cli PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../include)
add_dependencies(fbsd-cli fastbuild-scan-deps)
