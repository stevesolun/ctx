---
version: alpha
name: Therapy Room
description: Mental-health app: clay, breath-blue, hush.
colors:
  primary: "#2F2824"
  secondary: "#A3978C"
  tertiary: "#7FAEC7"
  neutral: "#F4ECE1"
  surface: "#FBF5EA"
  on-primary: "#FBF5EA"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 4rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.25rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.75
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 500
    letterSpacing: "0.1em"
rounded:
  sm: 12px
  md: 20px
  lg: 32px
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

A mental-health-app palette: clay warmth, soft breath-blue accent, whispered type.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2F2824`):** Headlines and core text.
- **Secondary (`#A3978C`):** Borders, captions, and metadata.
- **Tertiary (`#7FAEC7`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4ECE1`):** The page foundation.

## Typography

- **display:** Fraunces 4rem
- **h1:** Fraunces 2.25rem
- **body:** Inter 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
