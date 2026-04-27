---
version: alpha
name: Mutual Aid
description: Mutual-aid zine: risograph orange, handbill mono, masking tape.
colors:
  primary: "#1A1A18"
  secondary: "#666358"
  tertiary: "#F1562A"
  neutral: "#F3EBD5"
  surface: "#FBF4DE"
  on-primary: "#FBF4DE"
typography:
  display:
    fontFamily: Space Mono
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Space Mono
    fontSize: 2rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.98rem
    lineHeight: 1.65
  label:
    fontFamily: Space Mono
    fontSize: 0.72rem
    letterSpacing: "0.08em"
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

A nonprofit-zine palette: risograph orange punch, handbill mono, tape-yellow accents.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A1A18`):** Headlines and core text.
- **Secondary (`#666358`):** Borders, captions, and metadata.
- **Tertiary (`#F1562A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3EBD5`):** The page foundation.

## Typography

- **display:** Space Mono 3.75rem
- **h1:** Space Mono 2rem
- **body:** Inter 0.98rem
- **label:** Space Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
