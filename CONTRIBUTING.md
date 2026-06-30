# Contributing to smooth-linuxcnc

Thanks for your interest in contributing. This is a **reference client** for
[Smooth Core](https://github.com/loobric/smooth-core), licensed **MIT**.

## No CLA — just a DCO sign-off

Unlike the AGPL-licensed `smooth-core` server (which requires a Contributor
License Agreement), the MIT-licensed client repositories do **not** require a
CLA. Instead, we use the **Developer Certificate of Origin (DCO)**: a simple,
per-commit statement that you wrote the patch or otherwise have the right to
contribute it under the project's license.

You agree to the DCO (<https://developercertificate.org/>) by adding a
`Signed-off-by` line to each commit:

```
git commit -s -m "Your message"
```

This appends a trailer using your configured `git` name and email:

```
Signed-off-by: Jane Developer <jane@example.com>
```

Use your real name and a reachable email. CI checks every commit in a pull
request for this trailer and will fail the PR if any commit is missing it. To
fix an existing branch:

```
git rebase --signoff main
```

## Development

See the **Development** section in the [README](./README.md): the client is a
single file with stdlib-only tests (`unittest`), and `examples/` contains a
LinuxCNC sim configuration for testing. Note the deliberate single-file design
constraint before proposing structural changes.

## Pull requests

- Reference the issue your change addresses.
- Keep changes focused and the test suite green.
- Be respectful in discussion.
