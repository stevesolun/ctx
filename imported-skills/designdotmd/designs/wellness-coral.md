---
version: alpha
name: Wellness Coral
description: Sunrise coral, cotton grey, breathwork pastels.
colors:
  primary: "#2B2724"
  secondary: "#A19890"
  tertiary: "#FF6E5C"
  neutral: "#FDF6F3"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Spectral
    fontSize: 4rem
    fontWeight: 400
  h1:
    fontFamily: Spectral
    fontSize: 2.25rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.1em"
rounded:
  sm: 10px
  md: 18px
  lg: 28px
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

A wellness-app palette that nudges without shouting.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2B2724`):** Headlines and core text.
- **Secondary (`#A19890`):** Borders, captions, and metadata.
- **Tertiary (`#FF6E5C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FDF6F3`):** The page foundation.

## Typography

- **display:** Spectral 4rem
- **h1:** Spectral 2.25rem
- **body:** Inter 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
