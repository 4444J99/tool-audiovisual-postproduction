# Branch and push rules

These rules apply to every contributor, human or agent, on this repository.

1. **No force pushes.** Not `--force`, not `--force-with-lease`, not `+ref` refspecs, on any branch, including a branch you
   alone created and have not shared. `--force-with-lease` only protects against overwriting commits you have not seen; it
   still rewrites published history and still discards the earlier commits from the branch.
2. **Update an open branch by merging, not rebasing.** When `main` has moved, run `git merge origin/main` on the branch
   and push the merge commit. A normal push then fast-forwards the remote branch. Resolve conflicts in the merge commit.
3. **Land with a rebase-merge** (`gh pr merge --rebase --match-head-commit <sha>`), so `main` stays linear without anyone
   rewriting a pushed branch. Squash or merge commits are also acceptable where the history needs them; rewriting a branch
   that has been pushed is not.
4. **A fix after review is a new commit.** Do not amend or squash locally and push over the old head. If a pushed commit
   is wrong, add a commit that corrects it.
5. **Never use `--admin`** to merge past a failing or pending check.

## Record: the force-with-lease breach (previous TouchDesigner lane, 2026-11-14)

During the previous TouchDesigner effects lane, a `git push --force-with-lease` was used to publish a rebased branch.
The branch was the lane's own and had not been shared with anyone, which is why it was thought to be safe. It was a
breach of rule 1 all the same: the remote branch was rewritten, and being unshared does not make a rewrite harmless
(the open pull request, its review comments anchored to earlier commits, and any clone or automation that had already
fetched the branch all depend on the published history staying put).

Evidence: this record is taken from the lane's own report to the orchestrator. GitHub's public timeline for pull
requests 148 to 158 shows no `head_ref_force_pushed` event and the repository's recent push events for the lane's
branches all compare as fast-forwards (the lane's earlier branch has since been deleted by the merge), so the breach is
not independently visible in the public record; the rule above is the remedy regardless.

The correct move in that situation was `git merge origin/main` into the branch, a plain push, and a rebase-merge when
the pull request was ready.
