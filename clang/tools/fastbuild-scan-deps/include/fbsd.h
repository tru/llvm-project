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
