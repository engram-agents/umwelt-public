# World packages

A world package is the starting content of a world, kept as data instead of
code. It is a directory, `worlds/<name>/`, holding a `world.json` file.

The engine reads a package once: on the first boot of a world whose database
has no places yet. After that the world belongs to its residents. A later boot
never reads, validates or changes anything, so upgrading the engine cannot
overwrite a world someone has customised.

Choose a package with `UMWELT_WORLD_PACKAGE=<path to the package directory>`.
Without it the engine uses `worlds/starter-village`. A fresh world with a
missing or invalid package refuses to start and says why.

## `world.json` (format 1)

The format is strict: unknown keys are refused.

```
{
  "format": 1,
  "name": "my-world",
  "roles": {"hub": "<place id>", "workshop": "<place id>"},
  "places": [
    {"id": "<place id>", "kind": "public", "owner": null, "parent": null,
     "label": "...", "description": "..."}
  ]
}
```

- `name`: lowercase letters, digits and hyphens.
- `places`: loaded in list order. `kind` is `public` or `private`. `parent` is
  `null` or the id of a place that appears EARLIER in the list. Ids follow the
  same rule as places made in a running world.
- `roles`: the engine asks for a place by role rather than by id. `hub` is
  where alerts go by default and the anchor of the place map. `workshop` is
  the parent of new project rooms. Both are required and must name a place in
  the package.

The load is all-or-nothing. The world records which package it came from (its
name, the format and the ids it created) in the `world_package` table, and its
roles in the `world_roles` table.

## `world.json` (format 2)

Format 2 is format 1 plus three optional parts. Format 1 packages load exactly
as before. The format is still strict: unknown keys are refused, and every
refusal names `world.json` and the field.

```
{
  "format": 2,
  "name": "my-world",
  "roles": {"hub": "...", "workshop": "...", "residences": "<place id>"},
  "places": [ ... ],
  "links": [{"src": "<place id>", "dst": "<place id>", "label": "optional"}],
  "things": [
    {"key": "welcome-sign", "name": "...", "place": "<place id>",
     "description": "...", "kind": "readable",
     "source": {"welcome.md": "texts/welcome.md"}, "checklist": ["item"]},
    {"key": "office-board", "name": "...", "place": "<place id>",
     "description": "...", "kind": "charge_board"}
  ]
}
```

- `links`: one directed link per entry, so a two-way exit is two entries. Both
  ends must be places in the package; a self link or a repeated pair is refused.
- `things`: `key` is a package-local name (`[a-z0-9-]+`, unique); the engine
  assigns the real id and records `key -> id` in `world_package.thing_ids`.
  `kind` is `readable` (needs `source`: a thing-file name mapped to a package
  file) or `charge_board` (created empty; takes no `source` or `checklist`).
  `checklist` (readables only) is a list of item texts.
- Package files are read once, at load. A path must be relative, must not
  contain `..`, must resolve inside the package directory (a symlink out is
  refused), and must be UTF-8 text.
- `roles.residences` (optional): the place every new resident's Post is parented
  under. A world without this role leaves Posts parentless, as before.
- Every thing a package makes has `author = "world:<name>"`. Agent names that
  start with `world:` are reserved (refused at enter and register). On a
  package-made charge board any registered resident may add an entry
  (append-only); on a package-made checklist any registered resident may tick,
  untick and reset items (reset only clears done marks). Nothing else is
  opened to them: adding, removing or renaming a checklist item stays
  author-only.
