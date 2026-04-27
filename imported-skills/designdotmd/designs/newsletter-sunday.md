---
version: alpha
name: Newsletter Sunday
description: Long-form Sunday edition: linen paper, reading serif.
colors:
  primary: "#1F1A12"
  secondary: "#7A7062"
  tertiary: "#9C6A2E"
  neutral: "#F5EFE2"
  surface: "#FBF6EA"
  on-primary: "#FBF6EA"
typography:
  display:
    fontFamily: Source Serif 4
    fontSize: 4.5rem
    fontWeight: 600
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Source Serif 4
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Source Serif 4
    fontSize: 1.08rem
    lineHeight: 1.75
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.12em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A long-form newsletter system: linen paper, deeply readable serif, warm ink.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1F1A12`):** Headlines and core text.
- **Secondary (`#7A7062`):** Borders, captions, and metadata.
- **Tertiary (`#9C6A2E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5EFE2`):** The page foundation.

## Typography

- **display:** Source Serif 4 4.5rem
- **h1:** Source Serif 4 2.4rem
- **body:** Source Serif 4 1.08rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
