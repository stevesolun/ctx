---
version: alpha
name: National Park
description: Park signage: pine green, canyon rust, trail ochre.
colors:
  primary: "#EFE2C4"
  secondary: "#A89274"
  tertiary: "#D37B3A"
  neutral: "#1C2E20"
  surface: "#253D2B"
  on-primary: "#EFE2C4"
typography:
  display:
    fontFamily: Merriweather
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Merriweather
    fontSize: 2.3rem
    fontWeight: 700
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Merriweather
    fontSize: 0.72rem
    fontWeight: 700
    letterSpacing: "0.16em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A national-park palette: pine-green surface, canyon-rust accent, trail-ochre markers.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#EFE2C4`):** Headlines and core text.
- **Secondary (`#A89274`):** Borders, captions, and metadata.
- **Tertiary (`#D37B3A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#1C2E20`):** The page foundation.

## Typography

- **display:** Merriweather 4.5rem
- **h1:** Merriweather 2.3rem
- **body:** Lora 1rem
- **label:** Merriweather 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
