---
version: alpha
name: Forest Trail
description: Trail-map forest: moss green, bark brown, trail red.
colors:
  primary: "#17251A"
  secondary: "#6D7D65"
  tertiary: "#B03D2E"
  neutral: "#EEE7D6"
  surface: "#F7F1E1"
  on-primary: "#F7F1E1"
typography:
  display:
    fontFamily: Work Sans
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Work Sans
    fontSize: 1.95rem
    fontWeight: 700
  body:
    fontFamily: Work Sans
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Work Sans
    fontSize: 0.74rem
    fontWeight: 700
    letterSpacing: "0.1em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
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

A trail-map palette: mossy greens and bark browns, marker-red for paths.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#17251A`):** Headlines and core text.
- **Secondary (`#6D7D65`):** Borders, captions, and metadata.
- **Tertiary (`#B03D2E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEE7D6`):** The page foundation.

## Typography

- **display:** Work Sans 3.5rem
- **h1:** Work Sans 1.95rem
- **body:** Work Sans 1rem
- **label:** Work Sans 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
