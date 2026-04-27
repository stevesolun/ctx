---
version: alpha
name: Law Chambers
description: Chambers: oxblood, foolscap cream, barrister grey.
colors:
  primary: "#1A0E0C"
  secondary: "#78665E"
  tertiary: "#7A1020"
  neutral: "#F2ECDA"
  surface: "#FBF6E6"
  on-primary: "#FBF6E6"
typography:
  display:
    fontFamily: Source Serif 4
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Source Serif 4
    fontSize: 2.2rem
    fontWeight: 700
  body:
    fontFamily: Source Serif 4
    fontSize: 1.05rem
    lineHeight: 1.75
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 700
    letterSpacing: "0.22em"
rounded:
  sm: 0px
  md: 2px
  lg: 4px
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

A legal-firm palette: foolscap cream surface, oxblood accent, barrister-grey secondary.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A0E0C`):** Headlines and core text.
- **Secondary (`#78665E`):** Borders, captions, and metadata.
- **Tertiary (`#7A1020`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2ECDA`):** The page foundation.

## Typography

- **display:** Source Serif 4 4rem
- **h1:** Source Serif 4 2.2rem
- **body:** Source Serif 4 1.05rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
