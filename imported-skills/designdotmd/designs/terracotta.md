---
version: alpha
name: Terracotta
description: Sun-baked clay, ink, and weathered paper.
colors:
  primary: "#2B1D14"
  secondary: "#8B6F5B"
  tertiary: "#C56A3C"
  neutral: "#F3E8D8"
  surface: "#FBF4E7"
  on-primary: "#FBF4E7"
typography:
  display:
    fontFamily: DM Serif Display
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: DM Serif Display
    fontSize: 2.75rem
    fontWeight: 400
  body:
    fontFamily: DM Sans
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: DM Sans
    fontSize: 0.75rem
    letterSpacing: "0.1em"
rounded:
  sm: 4px
  md: 8px
  lg: 16px
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

A slow, sun-drenched palette for long-form reading. Cream background, ink headlines, and terracotta for emphasis.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2B1D14`):** Headlines and core text.
- **Secondary (`#8B6F5B`):** Borders, captions, and metadata.
- **Tertiary (`#C56A3C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3E8D8`):** The page foundation.

## Typography

- **display:** DM Serif Display 4.5rem
- **h1:** DM Serif Display 2.75rem
- **body:** DM Sans 1.05rem
- **label:** DM Sans 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
