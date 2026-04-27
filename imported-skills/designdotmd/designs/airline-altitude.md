---
version: alpha
name: Airline Altitude
description: Boarding-pass minimal: sky grey, contrail teal.
colors:
  primary: "#15222D"
  secondary: "#6B7884"
  tertiary: "#00A3B4"
  neutral: "#EFF3F6"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 3.75rem
    fontWeight: 500
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 2rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Space Grotesk
    fontSize: 0.72rem
    fontWeight: 500
    letterSpacing: "0.14em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

An airline-brand palette: sky grey surfaces, contrail teal, disciplined sans.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#15222D`):** Headlines and core text.
- **Secondary (`#6B7884`):** Borders, captions, and metadata.
- **Tertiary (`#00A3B4`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EFF3F6`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.75rem
- **h1:** Space Grotesk 2rem
- **body:** Inter 0.95rem
- **label:** Space Grotesk 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
