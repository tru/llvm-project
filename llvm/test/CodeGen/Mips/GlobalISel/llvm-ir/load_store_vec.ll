; NOTE: Assertions have been autogenerated by utils/update_llc_test_checks.py
; RUN: llc  -O0 -mtriple=mipsel-linux-gnu -global-isel -mcpu=mips32r5 -mattr=msa,+fp64 -mattr=nan2008 -verify-machineinstrs %s -o -| FileCheck %s -check-prefixes=P5600

define void @load_store_v16i8(<16 x i8>* %a, <16 x i8>* %b) {
; P5600-LABEL: load_store_v16i8:
; P5600:       # %bb.0: # %entry
; P5600-NEXT:    ld.b $w0, 0($5)
; P5600-NEXT:    st.b $w0, 0($4)
; P5600-NEXT:    jr $ra
; P5600-NEXT:    nop
entry:
  %0 = load <16 x i8>, <16 x i8>* %b, align 16
  store <16 x i8> %0, <16 x i8>* %a, align 16
  ret void
}

define void @load_store_v8i16(<8 x i16>* %a, <8 x i16>* %b) {
; P5600-LABEL: load_store_v8i16:
; P5600:       # %bb.0: # %entry
; P5600-NEXT:    ld.h $w0, 0($5)
; P5600-NEXT:    st.h $w0, 0($4)
; P5600-NEXT:    jr $ra
; P5600-NEXT:    nop
entry:
  %0 = load <8 x i16>, <8 x i16>* %b, align 16
  store <8 x i16> %0, <8 x i16>* %a, align 16
  ret void
}

define void @load_store_v4i32(<4 x i32>* %a, <4 x i32>* %b) {
; P5600-LABEL: load_store_v4i32:
; P5600:       # %bb.0: # %entry
; P5600-NEXT:    ld.w $w0, 0($5)
; P5600-NEXT:    st.w $w0, 0($4)
; P5600-NEXT:    jr $ra
; P5600-NEXT:    nop
entry:
  %0 = load <4 x i32>, <4 x i32>* %b, align 16
  store <4 x i32> %0, <4 x i32>* %a, align 16
  ret void
}

define void @load_store_v2i64(<2 x i64>* %a, <2 x i64>* %b) {
; P5600-LABEL: load_store_v2i64:
; P5600:       # %bb.0: # %entry
; P5600-NEXT:    ld.d $w0, 0($5)
; P5600-NEXT:    st.d $w0, 0($4)
; P5600-NEXT:    jr $ra
; P5600-NEXT:    nop
entry:
  %0 = load <2 x i64>, <2 x i64>* %b, align 16
  store <2 x i64> %0, <2 x i64>* %a, align 16
  ret void
}

define void @load_store_v4f32(<4 x float>* %a, <4 x float>* %b) {
; P5600-LABEL: load_store_v4f32:
; P5600:       # %bb.0: # %entry
; P5600-NEXT:    ld.w $w0, 0($5)
; P5600-NEXT:    st.w $w0, 0($4)
; P5600-NEXT:    jr $ra
; P5600-NEXT:    nop
entry:
  %0 = load <4 x float>, <4 x float>* %b, align 16
  store <4 x float> %0, <4 x float>* %a, align 16
  ret void
}

define void @load_store_v2f64(<2 x double>* %a, <2 x double>* %b) {
; P5600-LABEL: load_store_v2f64:
; P5600:       # %bb.0: # %entry
; P5600-NEXT:    ld.d $w0, 0($5)
; P5600-NEXT:    st.d $w0, 0($4)
; P5600-NEXT:    jr $ra
; P5600-NEXT:    nop
entry:
  %0 = load <2 x double>, <2 x double>* %b, align 16
  store <2 x double> %0, <2 x double>* %a, align 16
  ret void
}
