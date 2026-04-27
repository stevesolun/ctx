---
version: alpha
name: Meditation Zen
description: Sitting practice: bone paper, breath blue, ink brush.
colors:
  primary: "#1F1E1A"
  secondary: "#8C867A"
  tertiary: "#6894A8"
  neutral: "#F4EFE3"
  surface: "#FBF6EA"
  on-primary: "#FBF6EA"
typography:
  display:
    fontFamily: Spectral
    fontSize: 4rem
    fontWeight: 300
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Spectral
    fontSize: 2.2rem
    fontWeight: 300
  body:
    fontFamily: Spectral
    fontSize: 1.05rem
    lineHeight: 1.8
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 400
    letterSpacing: "0.16em"
rounded:
  sm: 14px
  md: 24px
  lg: 40px
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

A meditation-app palette: bone surface, breath-blue accent, brush-stroke serif.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1F1E1A`):** Headlines and core text.
- **Secondary (`#8C867A`):** Borders, captions, and metadata.
- **Tertiary (`#6894A8`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4EFE3`):** The page foundation.

## Typography

- **display:** Spectral 4rem
- **h1:** Spectral 2.2rem
- **body:** Spectral 1.05rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
