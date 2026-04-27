---
version: alpha
name: Desert Rose
description: Sandstone, dusty rose, sunburnt ink.
colors:
  primary: "#1F1410"
  secondary: "#9E7B6E"
  tertiary: "#D97B5A"
  neutral: "#F5E6D8"
  surface: "#FBF1E5"
  on-primary: "#FBF1E5"
typography:
  display:
    fontFamily: Libre Caslon Display
    fontSize: 4.75rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Libre Caslon Display
    fontSize: 2.75rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    letterSpacing: "0.1em"
rounded:
  sm: 4px
  md: 10px
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

A slow-burn desert palette: sandstone surfaces, dusty rose accent, ink black for counterweight. Feels like late afternoon.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1F1410`):** Headlines and core text.
- **Secondary (`#9E7B6E`):** Borders, captions, and metadata.
- **Tertiary (`#D97B5A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5E6D8`):** The page foundation.

## Typography

- **display:** Libre Caslon Display 4.75rem
- **h1:** Libre Caslon Display 2.75rem
- **body:** Inter 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
