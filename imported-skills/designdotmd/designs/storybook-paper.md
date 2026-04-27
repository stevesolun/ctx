---
version: alpha
name: Storybook Paper
description: Children's storybook: butter paper, crayon scribbles.
colors:
  primary: "#2E1E0F"
  secondary: "#9C8870"
  tertiary: "#F05A5A"
  neutral: "#FFF3CF"
  surface: "#FFF9E0"
  on-primary: "#2E1E0F"
typography:
  display:
    fontFamily: Caveat
    fontSize: 5.5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.5rem
    fontWeight: 700
  body:
    fontFamily: Fraunces
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: Caveat
    fontSize: 1rem
    fontWeight: 700
    letterSpacing: "0.02em"
rounded:
  sm: 8px
  md: 14px
  lg: 24px
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

A children's-book palette: butter paper surface, crayon-scribble illustration feel, friendly serif.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2E1E0F`):** Headlines and core text.
- **Secondary (`#9C8870`):** Borders, captions, and metadata.
- **Tertiary (`#F05A5A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FFF3CF`):** The page foundation.

## Typography

- **display:** Caveat 5.5rem
- **h1:** Fraunces 2.5rem
- **body:** Fraunces 1.05rem
- **label:** Caveat 1rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
