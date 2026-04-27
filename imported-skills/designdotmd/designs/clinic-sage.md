---
version: alpha
name: Clinic Sage
description: Clinical calm. Sage greens, paper white, zero alarm.
colors:
  primary: "#1B3A2E"
  secondary: "#7A8F85"
  tertiary: "#4E8B6A"
  neutral: "#F4F7F4"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: DM Sans
    fontSize: 3.5rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: DM Sans
    fontSize: 2rem
    fontWeight: 500
  body:
    fontFamily: DM Sans
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: DM Sans
    fontSize: 0.75rem
    fontWeight: 500
    letterSpacing: "0.06em"
rounded:
  sm: 6px
  md: 10px
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

A patient-portal palette designed to lower blood pressure.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1B3A2E`):** Headlines and core text.
- **Secondary (`#7A8F85`):** Borders, captions, and metadata.
- **Tertiary (`#4E8B6A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4F7F4`):** The page foundation.

## Typography

- **display:** DM Sans 3.5rem
- **h1:** DM Sans 2rem
- **body:** DM Sans 1rem
- **label:** DM Sans 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
