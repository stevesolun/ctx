---
version: alpha
name: Glassline
description: Fog-grey neutrals with a cobalt pinprick.
colors:
  primary: "#0F1419"
  secondary: "#4A5568"
  tertiary: "#2C5EF5"
  neutral: "#F1F3F5"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Geist
    fontSize: 3.75rem
    fontWeight: 600
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Geist
    fontSize: 2.25rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  body:
    fontFamily: Geist
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Geist Mono
    fontSize: 0.75rem
    letterSpacing: "0"
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

A cool, quiet palette built around 8-step neutrals. A single cobalt for action keeps the interface calm but directable.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F1419`):** Headlines and core text.
- **Secondary (`#4A5568`):** Borders, captions, and metadata.
- **Tertiary (`#2C5EF5`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F1F3F5`):** The page foundation.

## Typography

- **display:** Geist 3.75rem
- **h1:** Geist 2.25rem
- **body:** Geist 0.95rem
- **label:** Geist Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
