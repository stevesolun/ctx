---
version: alpha
name: Science Journal
description: Scientific-journal: paper-white column, abstract-blue plates.
colors:
  primary: "#121213"
  secondary: "#606368"
  tertiary: "#1A4B8C"
  neutral: "#FAF9F5"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Source Serif 4
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Source Serif 4
    fontSize: 2.2rem
    fontWeight: 700
  body:
    fontFamily: Source Serif 4
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: IBM Plex Sans
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.08em"
rounded:
  sm: 2px
  md: 3px
  lg: 5px
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

A scientific-journal palette: paper-white columns, plate-blue chart accent, strict rag-right.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#121213`):** Headlines and core text.
- **Secondary (`#606368`):** Borders, captions, and metadata.
- **Tertiary (`#1A4B8C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FAF9F5`):** The page foundation.

## Typography

- **display:** Source Serif 4 4rem
- **h1:** Source Serif 4 2.2rem
- **body:** Source Serif 4 1rem
- **label:** IBM Plex Sans 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
