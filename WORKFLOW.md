# How we work in this repo

Three people, one weekend, one demo. This document exists so nobody ever
overwrites anybody else's work, and so the thing we show the judges always runs.

Read the first two sections. The rest is reference for when something breaks.

---

## 1. The branch layout

There are three kinds of branch. That is all.

```
main              ← the demo. Always works. Protected. Nobody pushes here.
 └── dev          ← where everyone's work comes together. Default branch.
      ├── feat/krish/route-scoring
      ├── feat/niko/map-ui
      └── feat/alex/weather-api
```

**`main`** — the version we would show a judge *right now* if they walked over.
It only ever changes when we deliberately release from `dev`. GitHub is
configured to reject direct pushes to it, so you cannot break this rule by
accident.

**`dev`** — the shared integration branch, and the repo's **default branch**.
When you clone the repo you land here. When you open a pull request it targets
this by default. Everyone's finished work merges here first.

**`feat/<yourname>/<thing>`** — your personal workspace. You make one per task,
you are the only person who touches it, and it gets deleted after it merges.
Your name is in the branch so that `git branch -r` tells us at a glance who is
mid-flight on what.

Naming: all lowercase, dashes not spaces.
Good: `feat/niko/map-clustering`, `fix/alex/gpx-export-crash`
Bad: `Niko's branch`, `test2`, `new-stuff`

---

## 2. The daily loop

This is the whole workflow. Five commands, in this order, every time.

### Starting something new

```bash
git checkout dev          # go to the shared branch
git pull                  # get everyone else's latest work
git checkout -b feat/krish/route-scoring    # branch off it (use YOUR name)
```

> Always branch off a **freshly pulled `dev`**. Branching off a stale `dev`
> is the single most common cause of painful merge conflicts.

### While you work

Commit early, commit often. A commit is a save point, not a finished feature.

```bash
git add .
git commit -m "add elevation weighting to route scorer"
git push                  # first push will suggest --set-upstream, just do what it says
```

Push at least once before you stop for the day, even if it is unfinished. Work
that only exists on your laptop is work the team cannot see and cannot recover.

### When it is ready for the team

```bash
gh pr create --base dev --fill
```

(Or click "Compare & pull request" on GitHub — same thing.)

Then **post the PR link in the team chat** and tag whoever should look at it.
A PR nobody knows about is a PR nobody reviews.

### Getting it merged

- One other person reviews and approves. Not both — one is enough, we are three
  people and one of them wrote it.
- **The reviewer or the author clicks merge. A human always does the merging.**
- After merging, delete the branch (GitHub offers a button).

### Then start clean

```bash
git checkout dev
git pull
```

Do this after *every* merge, yours or anyone else's. Cheap habit, saves hours.

---

## 3. Staying in sync while you work

If you are on a branch for more than a few hours, other people's work is piling
up in `dev` and drifting away from you. Pull it into your branch:

```bash
git checkout dev
git pull
git checkout feat/krish/route-scoring
git merge dev
```

Do this **once a day minimum**, and always right before you open your PR.
Resolving a small conflict on your own branch today is far easier than
resolving a large one in a PR at 2am.

---

## 4. Avoiding conflicts in the first place

Branch structure does not actually prevent merge conflicts. **Two people editing
the same file** causes merge conflicts. Everything below is about that.

### The one rule that matters: one owner per area

**As soon as we pick the tech stack, we split the codebase into top-level
directories and assign exactly one owner to each.** Fill this table in at that
point — it is currently empty because there is no code yet:

| Directory | Owner | What lives here |
| --------- | ----- | --------------- |
| _(TBD)_   |       |                 |
| _(TBD)_   |       |                 |
| _(TBD)_   |       |                 |

Then: **you edit inside your own directory freely, and you never edit inside
someone else's without telling them.** If your change genuinely has to cross a
boundary — you need a new field on a shared type, you need to change a function
signature someone else calls — then:

1. Say so in the team chat *before* you write it, not after.
2. Call it out explicitly at the top of the PR description:
   `Touches niko/ — changed the Route type to carry an elevation profile.`

### Shared files are the danger zone

Files everybody has to touch (dependency manifests, shared type definitions,
config) are where conflicts actually happen. When you edit one:

- Make the change small and merge it fast. Do not sit on it for a day.
- Say in chat that you are touching it.

### Small PRs beat big PRs

A PR with 60 changed lines gets reviewed in three minutes and merges cleanly.
A PR with 2,000 changed lines sits for six hours and conflicts with everything.
Under a hackathon deadline, merge frequency matters more than tidiness.

---

## 5. Releasing to `main`

Do this when `dev` is in a state we would be happy to demo — and **definitely
once well before the final presentation**, so the demo version is frozen and
safe while people keep hacking on `dev`.

```bash
gh pr create --base main --head dev --title "Release: <what's in it>"
```

Someone approves it, a human merges it. That is the only way code reaches
`main`.

Rule of thumb: if `dev` is broken, fine, fix it. If `main` is broken during
judging, that is the whole hackathon.

---

## 6. When something goes wrong

### "Your branch is behind" / push rejected

Someone pushed before you. Pull their work, then push yours:

```bash
git pull
git push
```

### Merge conflict

Git is telling you two people changed the same lines and it cannot guess which
one wins. This is normal and not a disaster.

```bash
git status                # lists the conflicted files
```

Open each one. You will see:

```
<<<<<<< HEAD
your version
=======
their version
>>>>>>> dev
```

Delete the `<<<<<<<`, `=======` and `>>>>>>>` marker lines and leave the code
you actually want — sometimes yours, sometimes theirs, often a combination of
both. Then:

```bash
git add .
git commit
```

**If the conflict is in someone else's code, go ask them.** Guessing which
version to keep is how you silently delete a teammate's work.

### "I committed to the wrong branch"

You are on `dev` (or `main`) and realise your commits should have been on a
feature branch. Nothing is lost:

```bash
git checkout -b feat/krish/oops   # carries your commits onto a new branch
git checkout dev
git reset --hard origin/dev       # resets dev back to match the remote
```

> `git reset --hard` throws away uncommitted changes on that branch. Only run it
> after the `checkout -b` above has safely parked your commits somewhere.

### "I need to switch branches but I'm mid-change"

Park your work without committing it:

```bash
git stash            # tuck the changes away
git checkout dev     # go do the other thing
git checkout -       # come back
git stash pop        # get the changes back
```

### It's really broken and you're out of ideas

Your work still exists — pushed branches do not vanish. Ask in chat before
running any command you found online that contains `--force`. Force-pushing to
a shared branch is the one mistake that genuinely destroys other people's work,
which is why GitHub blocks it on `main` and `dev`.

---

## 7. First-time setup

Once per person, per machine:

```bash
git clone https://github.com/krishaanth5831/BMW-Ride-Planner.git
cd BMW-Ride-Planner
git config user.name "Your Name"
git config user.email "your@email.com"
```

You will land on `dev` — that is correct, that is the default branch.

To use `gh pr create`, install the GitHub CLI (`gh`) and run `gh auth login`
once. Optional; the GitHub website does the same job.

---

## 8. The rules, in one place

1. Never push directly to `main`. (GitHub enforces this.)
2. Never push directly to `dev` — go through a PR.
3. Always branch off a freshly pulled `dev`.
4. One PR per task, reviewed by one other person, merged by a human.
5. Push your work before you stop for the day, finished or not.
6. Merge `dev` into your branch daily and before every PR.
7. Stay inside your own directory; announce it when you cannot.
8. Never `--force` anything on a shared branch.
