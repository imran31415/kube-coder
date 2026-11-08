# Publish a completed Build

An isolated Build keeps its branch and files after its agent stops. Open **Changes → Prepare review** to review that work and publish it from the dashboard or phone.

1. End the Build session. A waiting agent or an interactive shell is still a live session.
2. Prepare a review. Check the head repository/branch, base repository/branch, Git author, changed files and snapshot diffs.
3. Read the generated title and summary. Edit them and use **Save description** to retain edits when closing the screen. **Create as draft PR** keeps the PR out of the ready-for-review state.
4. Tap **Push & Open PR**. The server commits the reviewed snapshot if needed, pushes its exact commit, and creates or discovers the matching PR. The operation continues if the phone disconnects.
5. Reopen Changes to see saved progress or **Open PR**. For more changes on an open PR, refresh the review and choose **Push updates**.

The feature never merges a PR. Existing agent-created PRs are discovered by head repository/branch and base. A closed or merged PR blocks further publishing from that branch; start another Build for new work.

## Review and test results

Preparation uses a temporary Git index. It includes tracked modifications, deletions and non-ignored new files without changing the real staging area. Diffs refer to the prepared Git tree, even if working files change afterward. Publication checks the snapshot, owner, branch, staging area, destination and account again. A changed snapshot requires a fresh review.

Test output from the Build log is historical evidence. It cannot prove that the exact reviewed revision passed. The UI and generated PR description say when results are unknown or when the log reports failures. Publishing does not run tests or silently claim success.

The AI only drafts text. Its tools, MCP integrations, repository settings and hooks are disabled. If generation is unavailable, the description remains editable and publishing still works.

## Phone behavior

Buttons and file rows have at least 44-point touch targets. Descriptions saved on the server survive closing the sheet or app. A competing saved edit returns a conflict instead of overwriting it. In-progress publication disables repeat actions, and repeated requests are reconciled with the saved operation.

Readiness, success and actionable failures appear in Feed. When push is enabled, publishing notifications target the Build's Changes screen. Cold-start taps wait for connection hydration and navigation. A notification from another workspace opens Feed instead of a same-named Build in the current workspace. Older servers omit the publishing controls.

## Destinations and credentials

Publishing uses the workspace's selected GitHub App or personal account and refreshes its credentials for network operations. Configure the Git author in Settings. No default owner, `main`, or `origin` is assumed: the Build base and Git push configuration determine the initial destination. **Destination options** can select a configured push remote and base branch. Forks target their parent repository by default.

Only GitHub.com HTTPS and SSH remote identities are recognized. Transport uses HTTPS with scoped credentials; embedded credentials in remote URLs are rejected. Push is a normal fast-forward push, never a force push. Git commit signing configuration is honored. Repository hooks are disabled in the server-owned operation.

## Recovery and limits

**Retry saved publish** checks the recorded commit, remote ref and matching PR before repeating an external write. If push succeeded but PR creation failed, retry reuses the commit. Server restart also resumes queued operations. Interrupted review preparation can be started again without losing the previous saved description.

Do not delete a retained publishing record or an ambiguous Git index lock to make an error disappear. A recovery-required message means the service refused to guess about ownership or staging changes. Preserve the worktree and record for diagnosis. Cleanup, worktree reuse and managed agent launches are blocked while a publication reservation is held.

Shared checkouts, detached HEAD, active merges/rebases, submodule changes, sparse checkouts, LFS/custom filters, and oversized changes require another workflow. The initial limits are 1,000 changed files and 32 MiB of changed working-file content. Displayed diffs are truncated at 256 KiB. These checks do not prevent someone with direct shell access from changing files; late working-file edits are retained, and a changed branch or index blocks publication.

Publishing state is stored on the persistent volume under the task directory's `publishing/` folder, or `KC_PUBLISH_DIR`. Back it up with workspace data. Deploy the workspace image containing these Python modules and the rebuilt dashboard; changing a ConfigMap alone does not install the feature.
