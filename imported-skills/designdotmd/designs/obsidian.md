---
version: alpha
name: Obsidian
description: Volcanic glass, phosphor purple, edge.
colors:
  primary: "#E9E6F2"
  secondary: "#8B8699"
  tertiary: "#A78BFA"
  neutral: "#13111C"
  surface: "#1C1829"
  on-primary: "#13111C"
typography:
  display:
    fontFamily: Inter
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter
    fontSize: 2.25rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 6px
  md: 10px
  lg: 14px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A knowledge-worker's dark palette. Deep obsidian surfaces, violet accent, monospace labels. Calm but insistent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E9E6F2`):** Headlines and core text.
- **Secondary (`#8B8699`):** Borders, captions, and metadata.
- **Tertiary (`#A78BFA`):** The sole driver for interaction. Reserve it.
- **Neutral (`#13111C`):** The page foundation.

## Typography

- **display:** Inter 3.75rem
- **h1:** Inter 2.25rem
- **body:** Inter 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
