#include "fbsd.h"
#include "gtest/gtest.h"

TEST(FbsdSmoke, HeaderVersion) { EXPECT_EQ(FBSD_API_VERSION, 1); }
