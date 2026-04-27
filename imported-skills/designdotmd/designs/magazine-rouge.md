---
version: alpha
name: Magazine Rouge
description: Fashion-magazine red, ivory spreads, giant display.
colors:
  primary: "#161615"
  secondary: "#7E7A73"
  tertiary: "#C1172F"
  neutral: "#F4EFE7"
  surface: "#FAF6EE"
  on-primary: "#FAF6EE"
typography:
  display:
    fontFamily: Bodoni Moda
    fontSize: 6rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Bodoni Moda
    fontSize: 3rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.7rem
    fontWeight: 700
    letterSpacing: "0.22em"
rounded:
  sm: 0px
  md: 0px
  lg: 0px
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

An oversized-editorial system inspired by quarterly style titles.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#161615`):** Headlines and core text.
- **Secondary (`#7E7A73`):** Borders, captions, and metadata.
- **Tertiary (`#C1172F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4EFE7`):** The page foundation.

## Typography

- **display:** Bodoni Moda 6rem
- **h1:** Bodoni Moda 3rem
- **body:** Inter 1rem
- **label:** Inter 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
