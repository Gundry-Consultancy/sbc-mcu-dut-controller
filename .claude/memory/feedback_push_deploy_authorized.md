---
name: feedback-push-deploy-authorized
description: Standing authorization — may push to git and SSH to the bench for deploys without per-action confirmation
metadata:
  type: feedback
---

The user has granted standing authorization for this project: **push to git
without asking**, and **attempt SSH to the bench (tachyon `192.168.1.169` and
nested DUT hosts) at any time for deploys** without per-action confirmation.

**Why:** the user drives a tight commit→push→deploy→live-test loop on the HIL
pipeline; pausing to confirm each push/deploy slows it down.

**How to apply:** push controller `main` and WS PR branches without a
confirmation prompt; run the tachyon deploy (`git fetch && merge --ff-only
origin/main && sudo systemctl restart hil-controller`) when a controller change
needs to go live. Still *report* what was pushed/deployed and any live-run that
auto-triggers (e.g. a WS PR push fires the HIL workflow). This covers routine
push/deploy only — it does not extend to destructive ops or anything outside the
normal pipeline loop. See [[hil-ci-pipeline-state]].
