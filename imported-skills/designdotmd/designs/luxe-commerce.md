---
version: alpha
name: Luxe Commerce
description: DTC luxury: porcelain, bone ink, brushed copper.
colors:
  primary: "#141212"
  secondary: "#7F7770"
  tertiary: "#B07750"
  neutral: "#F3EEE5"
  surface: "#FBF7EF"
  on-primary: "#FBF7EF"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 5rem
    fontWeight: 300
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.5rem
    fontWeight: 300
  body:
    fontFamily: Jost
    fontSize: 0.98rem
    lineHeight: 1.65
  label:
    fontFamily: Jost
    fontSize: 0.72rem
    fontWeight: 400
    letterSpacing: "0.24em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
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

A DTC luxury-ecommerce palette: porcelain surface, bone-black ink, brushed-copper accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#141212`):** Headlines and core text.
- **Secondary (`#7F7770`):** Borders, captions, and metadata.
- **Tertiary (`#B07750`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3EEE5`):** The page foundation.

## Typography

- **display:** Fraunces 5rem
- **h1:** Fraunces 2.5rem
- **body:** Jost 0.98rem
- **label:** Jost 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
