# RUN: llvm-mc -triple=powerpc-linux-musl %s | FileCheck --check-prefix=PRINT %s

# RUN: llvm-mc -filetype=obj -triple=powerpc-linux-musl %s | llvm-readobj -r | FileCheck %s

# PRINT: .reloc 8, R_PPC_NONE, .data
# PRINT: .reloc 4, R_PPC_NONE, foo+4
# PRINT: .reloc 0, R_PPC_NONE, 8

# CHECK:      0x8 R_PPC_NONE .data 0x0
# CHECK-NEXT: 0x4 R_PPC_NONE foo 0x4
# CHECK-NEXT: 0x0 R_PPC_NONE - 0x8

.text
  blr
  nop
  nop
  .reloc 8, R_PPC_NONE, .data
  .reloc 4, R_PPC_NONE, foo+4
  .reloc 0, R_PPC_NONE, 8

.data
.globl foo
foo:
  .word 0
  .word 0
  .word 0
