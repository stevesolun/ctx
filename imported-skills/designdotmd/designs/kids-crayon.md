---
version: alpha
name: Kids Crayon
description: Crayon-bright: primary colors, chunky radii, silly big type.
colors:
  primary: "#1B2A6B"
  secondary: "#6A75B8"
  tertiary: "#FFB400"
  neutral: "#EAF3FF"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Fredoka
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Fredoka
    fontSize: 2.4rem
    fontWeight: 700
  body:
    fontFamily: Fredoka
    fontSize: 1.05rem
    lineHeight: 1.55
  label:
    fontFamily: Fredoka
    fontSize: 0.82rem
    fontWeight: 600
    letterSpacing: "0.04em"
rounded:
  sm: 10px
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

A kids-product system with saturated primaries and jumbo touch targets.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1B2A6B`):** Headlines and core text.
- **Secondary (`#6A75B8`):** Borders, captions, and metadata.
- **Tertiary (`#FFB400`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EAF3FF`):** The page foundation.

## Typography

- **display:** Fredoka 4.5rem
- **h1:** Fredoka 2.4rem
- **body:** Fredoka 1.05rem
- **label:** Fredoka 0.82rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
