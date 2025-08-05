#!/usr/bin/env python3
# ===-- merge-release-pr.py --------------------------------------------------===#
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# ===------------------------------------------------------------------------===#

"""
Helper script that will merge a Pull Request into a release branch. It will first
do some validations of the PR then rebase and finally push the changes to the
release branch.

Usage: merge-release-pr.py <PR id>
By default it will push to the 'upstream' origin, but you can pass
--upstream-origin/-o <origin> if you want to change it.

If you want to skip a specific validation, like the status checks you can
pass -s status_checks, this argument can be passed multiple times.
"""

import argparse
import json
import subprocess
import sys
from typing import List


class PRMerger:
    def __init__(self, args):
        self.args = args

    def run_gh(self, gh_cmd: str, args: List[str]) -> str:
        cmd = ["gh", gh_cmd, "-Rllvm/llvm-project"] + args
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            print(p.stderr)
            raise RuntimeError("Failed to run gh")
        return p.stdout

    def validate_state(self, data):
        state = data["state"]
        if state != "OPEN":
            return False, f"state is {state.lower()}, not open"
        return True

    def validate_target_branch(self, data):
        base_ref_name: str = data["baseRefName"]
        if not base_ref_name.startswith("release/"):
            return False, f"target branch is {base_ref_name}, not a release branch"
        return True

    def validate_approval(self, data):
        if data["reviewDecision"] != "APPROVED":
            return False, "PR is not approved"
        return True

    def validate_status_checks(self, data):
        failures = []
        pending = []
        for status in data["statusCheckRollup"]:
            if "conclusion" in status and status["conclusion"] == "FAILURE":
                failures.append(status)
            if "status" in status and status["status"] == "IN_PROGRESS":
                pending.append(status)

        if failures or pending:
            errstr = "\n"
            if failures:
                errstr += "    FAILED: "
                errstr += ", ".join([d["name"] for d in failures])
            if pending:
                if failures:
                    errstr += "\n"
                errstr += "    PENDING: "
                errstr += ", ".join([d["name"] for d in pending])
            return False, errstr

        return True

    def validate_commits(self, data):
        if len(data["commits"]) > 1:
            return False, f"More than 1 commit! {len(data['commits'])}"
        return True
    
    def validate_public_headers(self, data):
        """
        Validate that no public headers (in llvm/include or clang/include) are changed.
        This helps detect potential ABI breakage risks.
        """
        # Get the diff from the PR branch compared to the base
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{self.args.upstream}/{self.target_branch}...HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            changed_files = result.stdout.strip().splitlines()
        except subprocess.CalledProcessError:
            return False, "Failed to run git diff"

        # Check for public header changes
        public_header_touched = [
            f for f in changed_files
            if f.startswith("llvm/include/") or f.startswith("clang/include/")
        ]

        if public_header_touched:
            return False, f"Public headers changed: {', '.join(public_header_touched)}"
        return True

    def _normalize_pr(self, parg: str):
        if parg.isdigit():
            return parg
        elif parg.startswith("https://github.com/llvm/llvm-project/pull"):
            i = parg[parg.rfind("/") + 1:]
            if not i.isdigit():
                raise RuntimeError(f"{i} is not a number, malformatted input.")
            return i
        else:
            raise RuntimeError(f"PR argument must be PR ID or pull request URL - {parg} is wrong.")

    def load_pr_data(self):
        self.args.pr = self._normalize_pr(self.args.pr)
        fields_to_fetch = [
            "baseRefName",
            "commits",
            "headRefName",
            "headRepository",
            "headRepositoryOwner",
            "reviewDecision",
            "state",
            "statusCheckRollup",
            "title",
            "url",
        ]
        print(f"> Loading PR {self.args.pr}...")
        o = self.run_gh("pr", ["view", self.args.pr, "--json", ",".join(fields_to_fetch)])
        self.prdata = json.loads(o)

        self.target_branch = self.prdata["baseRefName"]
        srepo = self.prdata["headRepository"]["name"]
        sowner = self.prdata["headRepositoryOwner"]["login"]
        self.source_url = f"https://github.com/{sowner}/{srepo}"
        self.source_branch = self.prdata["headRefName"]

        if srepo != "llvm-project":
            print("The target repo is NOT llvm-project, check the PR!")
            sys.exit(1)

        if sowner == "llvm":
            print("The source owner should never be github.com/llvm, double check the PR!")
            sys.exit(1)

    def validate_pr(self):
        print(f"> Handling PR {self.args.pr} - {self.prdata['title']}")
        print(f">   {self.prdata['url']}\n")
        print("> Validations:")
        total_ok = True
        validations = {
            "state": self.validate_state,
            "target_branch": self.validate_target_branch,
            "approval": self.validate_approval,
            "commits": self.validate_commits,
            "status_checks": self.validate_status_checks,
            "public_headers": self.validate_public_headers,
        }

        for val_name, val_func in validations.items():
            try:
                result = val_func(self.prdata)
            except Exception as e:
                result = False
            ok = None
            skipped = self.args.skip_validation and val_name in self.args.skip_validation
            if isinstance(result, bool) and result:
                ok = "OK"
            elif isinstance(result, tuple) and not result[0]:
                msg = result[1]
                ok = "SKIPPED: " + msg if skipped else "FAIL: " + msg
                if not skipped:
                    total_ok = False
            else:
                ok = "FAIL! (Unknown)"
                total_ok = False
            print(f"  * {val_name}: {ok}")

        return total_ok

    def checkout_pr(self):
        print("> Fetching PR changes...")
        self.merge_branch = "llvm_merger_" + self.args.pr
        self.run_gh("pr", ["checkout", self.args.pr, "--force", "--branch", self.merge_branch])

        result = subprocess.run(
            ["git", "config", f"branch.{self.merge_branch}.merge"],
            check=True, capture_output=True, text=True
        )
        upstream_branch = result.stdout.strip().replace("refs/heads/", "")
        print(upstream_branch)

    def rebase_pr(self):
        print("> Fetching upstream")
        subprocess.run(["git", "fetch", "--all", "-j10"], check=True)
        print("> Rebasing...")
        subprocess.run(["git", "rebase", f"{self.args.upstream}/{self.target_branch}"], check=True)
        print("> Publish rebase...")
        subprocess.run(["git", "push", "--force", self.source_url, f"HEAD:{self.source_branch}"])

    def squash_and_rebase(self):
        print("> Squashing all commits into the first one...")
        result = subprocess.run(
            ["git", "merge-base", f"{self.args.upstream}/{self.target_branch}", "HEAD"],
            capture_output=True, text=True, check=True
        )
        base = result.stdout.strip()

        result = subprocess.run(
            ["git", "rev-list", "--reverse", "--ancestry-path", f"{base}..HEAD"],
            capture_output=True, text=True, check=True
        )
        commits = result.stdout.strip().splitlines()
        if not commits:
            raise RuntimeError("Could not find any commits after merge base")
        first_commit = commits[0]
        print(f"> First commit on branch is {first_commit}")

        subprocess.run(["git", "reset", "--soft", first_commit], check=True)
        amend_args = ["git", "commit", "--amend"]
        if not self.args.edit_message:
            amend_args.append("--no-edit")
        subprocess.run(amend_args, check=True)

        print("> Publish rebase...")
        subprocess.run(["git", "push", "--force", self.source_url, f"HEAD:{self.source_branch}"])

        print("> Rebasing on top of target branch...")
        subprocess.run(
            ["git", "rebase", "--onto", f"{self.args.upstream}/{self.target_branch}", first_commit],
            check=True
        )

        print("> Publish rebase...")
        subprocess.run(["git", "push", "--force", self.source_url, f"HEAD:{self.source_branch}"])


    def push_upstream(self):
        print("> Pushing changes...")
        subprocess.run(["git", "push", self.args.upstream, f"HEAD:{self.target_branch}"], check=True)

    def delete_local_branch(self):
        print("> Deleting the old branch...")
        subprocess.run(["git", "switch", "main"], check=True)
        subprocess.run(["git", "branch", "-D", self.merge_branch], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pr", help="The Pull Request ID or URL")
    parser.add_argument(
        "--skip-validation", "-s", action="append",
        help="Skip specific validation(s) like -s status_checks"
    )
    parser.add_argument(
        "--upstream-origin", "-o", default="upstream", dest="upstream",
        help="The name of the origin to push to (default: upstream)"
    )
    parser.add_argument(
        "--no-push", action="store_true",
        help="Run validations, rebase, and fetch, but don't push"
    )
    parser.add_argument("--validate-only", action="store_true", help="Only run the validations")
    parser.add_argument("--rebase-only", action="store_true", help="Only rebase and exit")
    parser.add_argument("--squash-and-rebase", action="store_true", help="Squash all commits and rebase")
    parser.add_argument("--edit-message", action="store_true", help="Edit commit message during squash")

    args = parser.parse_args()
    merger = PRMerger(args)
    merger.load_pr_data()

    if args.rebase_only:
        merger.checkout_pr()
        merger.rebase_pr()
        merger.delete_local_branch()
        sys.exit(0)

    if args.squash_and_rebase:
        merger.checkout_pr()
        merger.squash_and_rebase()
        merger.delete_local_branch()
        sys.exit(0)

    if not merger.validate_pr():
        print("\n! Validations failed! Use --skip-validation to override.")
        sys.exit(1)

    if args.validate_only:
        print("\n! --validate-only passed, exiting.")
        sys.exit(0)

    merger.checkout_pr()
    merger.rebase_pr()

    if not args.no_push:
        merger.push_upstream()
    merger.delete_local_branch()

    print("\n> Done! Have a nice day!")


if __name__ == "__main__":
    main()
