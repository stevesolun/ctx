---
version: alpha
name: Architect Blueprint
description: Drafting-table blue, tracing paper, pencil-line grid.
colors:
  primary: "#0D2234"
  secondary: "#5A7589"
  tertiary: "#2E8FC4"
  neutral: "#E8EDF2"
  surface: "#F6F9FC"
  on-primary: "#F6F9FC"
typography:
  display:
    fontFamily: Unica One
    fontSize: 4rem
    fontWeight: 400
    letterSpacing: "0.04em"
  h1:
    fontFamily: Archivo
    fontSize: 2.1rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.12em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
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

An architect's-office system.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0D2234`):** Headlines and core text.
- **Secondary (`#5A7589`):** Borders, captions, and metadata.
- **Tertiary (`#2E8FC4`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E8EDF2`):** The page foundation.

## Typography

- **display:** Unica One 4rem
- **h1:** Archivo 2.1rem
- **body:** Inter 0.95rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
