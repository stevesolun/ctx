---
version: alpha
name: Natural Wine
description: Pét-nat: fresh lees, paper labels, soft burgundy.
colors:
  primary: "#2A0E19"
  secondary: "#7E5D65"
  tertiary: "#A8374F"
  neutral: "#F2EADA"
  surface: "#FAF3E3"
  on-primary: "#FAF3E3"
typography:
  display:
    fontFamily: Caveat
    fontSize: 5rem
    fontWeight: 700
  h1:
    fontFamily: Fraunces
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Fraunces
    fontSize: 1.02rem
    lineHeight: 1.7
  label:
    fontFamily: Fraunces
    fontSize: 0.75rem
    letterSpacing: "0.1em"
rounded:
  sm: 6px
  md: 12px
  lg: 20px
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

A bar-forward system inspired by natural-wine labels.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2A0E19`):** Headlines and core text.
- **Secondary (`#7E5D65`):** Borders, captions, and metadata.
- **Tertiary (`#A8374F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2EADA`):** The page foundation.

## Typography

- **display:** Caveat 5rem
- **h1:** Fraunces 2.4rem
- **body:** Fraunces 1.02rem
- **label:** Fraunces 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
